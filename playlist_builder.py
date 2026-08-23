"""
playlist_builder.py — Movierulz -> IPTV Playlist Builder
=======================================================
Scrapes Movierulz movie listings, extracts stream URLs, and builds
an IPTV-compatible M3U playlist compatible with OTT Navigator, TiviMate,
VLC, and any HLS player.

Usage:
    python playlist_builder.py                          # build and print
    python playlist_builder.py --output playlist.m3u    # build and save
    python playlist_builder.py --append                  # append new to existing

GitHub Actions:
    The update.yml workflow runs this script on a schedule,
    appends new movies to playlist.m3u, and commits the result.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

HEADERS = {"User-Agent": USER_AGENT}

PROXY_TEMPLATE = os.environ.get(
    "PROXY_TEMPLATE",
    "https://my-worker.workers.dev/proxy?url={url}",
)

MAX_LIST_PAGES = int(os.environ.get("MAX_LIST_PAGES", "500"))
POLITE_DELAY = float(os.environ.get("POLITE_DELAY", "0.5"))

# Max movies to extract streams for per run (to stay within timeout).
# The playlist grows incrementally over multiple runs.
MAX_STREAMS_PER_RUN = int(os.environ.get("MAX_STREAMS_PER_RUN", "50"))

MOVIERULZ_DOMAINS = [
    "https://www.5movierulz.watch",
    "https://www.5movierulz.viajes",
    "https://www.5movierulz.cfd",
    "https://www.5movierulz.green",
    "https://www.5movierulz.lat",
]

DEFAULT_CACHE_FILE = ".playlist_cache.json"
DEFAULT_PAGE_CACHE_FILE = ".page_cache.json"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(session, url, **kwargs):
    """GET with standard User-Agent and error handling."""
    try:
        resp = session.get(url, headers=HEADERS, timeout=15, **kwargs)
        return resp
    except requests.RequestException as exc:
        print(f"  [warn] Request failed: {url} ({exc})")
        return None


# ---------------------------------------------------------------------------
# Step 1: Discover movie listing pages
# ---------------------------------------------------------------------------

def discover_listing_pages(session, base_url, page_cache=None):
    """
    Find ALL paginated movie listing pages.
    Uses a page cache to skip already-scraped pages on subsequent runs.
    The site only links to pages 0 and 15 in pagination, but has 500+ pages.
    We start from the last linked page and keep going until we hit empty pages.
    """
    if page_cache is None:
        page_cache = {}

    for domain in MOVIERULZ_DOMAINS:
        resp = _get(session, domain)
        if not resp or resp.status_code != 200:
            continue

        html = resp.text
        listing_urls = []

        # Find the highest page number from pagination links
        page_links = set(re.findall(r'href=["\']([^"\']*\/page\/\d+[^"\']*)["\']', html))
        page_nums = []
        for link in page_links:
            m = re.search(r'/page/(\d+)', link)
            if m:
                page_nums.append(int(m.group(1)))

        if not page_nums:
            print("[scraper] No pagination found")
            return [], page_cache

        start_page = max(page_nums)
        print(f"[scraper] Pagination shows up to page {start_page}, discovering more...")

        # First pass: check which pages are already cached
        cached_pages = set(page_cache.keys())
        print(f"[scraper] {len(cached_pages)} pages already in cache")

        # Collect known page URLs (pages 1 to start_page from the linked range)
        for i in range(1, start_page + 1):
            page_url = f"{domain}/movies/page/{i}"
            if page_url not in cached_pages:
                listing_urls.append(page_url)

        # Keep fetching beyond the last linked page until empty
        consecutive_empty = 0
        page = start_page + 1
        while consecutive_empty < 3:
            page_url = f"{domain}/movies/page/{page}"
            if page_url in cached_pages:
                # We know this page exists, add it and continue
                listing_urls.append(page_url)
                page += 1
                continue

            resp = _get(session, page_url)
            if not resp or resp.status_code != 200:
                consecutive_empty += 1
                page += 1
                continue

            titles = re.findall(r'<a[^>]+title="([^"]+)"[^>]+href="(https?://[^"]+\.html)"', resp.text)
            valid_urls = [u for _, u in titles if MOVIE_URL_RE.match(u)]
            unique_count = len(set(valid_urls))

            if unique_count > 0:
                listing_urls.append(page_url)
                # Cache this page's movie URLs
                page_cache[page_url] = list(set(valid_urls))
                consecutive_empty = 0
            else:
                consecutive_empty += 1
            page += 1

        last_page = page - consecutive_empty - 1
        # Ensure all known pages from 1 to last_page are in the cache
        for i in range(1, last_page + 1):
            page_url = f"{domain}/movies/page/{i}"
            if page_url not in page_cache:
                page_cache[page_url] = []  # Mark as known (fetched during this run)

        new_pages = len(listing_urls)
        cached_count = last_page - new_pages
        print(f"[scraper] Will fetch {new_pages} new pages, {cached_count} from cache (total: {last_page})")
        return listing_urls, page_cache

    print("[scraper] Could not reach any Movierulz domain")
    return [], page_cache


# ---------------------------------------------------------------------------
# Step 2: Extract movie page links from listing pages
# ---------------------------------------------------------------------------

# Current URL pattern: /title-year-quality-language-ID.html
# e.g. /irumudi-2026-dvdscr-telugu-7377.html
MOVIE_URL_RE = re.compile(
    r'^https?://[^/]+/[a-z0-9]+-\d{4}-[a-z0-9]+-[a-z]+-\d+\.html$'
)


def extract_movie_links(session, listing_urls, page_cache=None):
    """
    Scrape each listing page and collect movie entries.
    Uses page_cache to skip already-scraped pages.
    """
    if page_cache is None:
        page_cache = {}

    movies = []
    seen_urls = set()

    for listing_url in listing_urls:
        # If this page is already cached, use cached URLs (no HTTP request)
        cached_urls = page_cache.get(listing_url)
        if cached_urls is not None:
            print(f"[scraper]   {listing_url} -> {len(cached_urls)} movies (from cache)")
            for url in cached_urls:
                if url not in seen_urls:
                    seen_urls.add(url)
                    title = _guess_title_from_url(url)
                    year = _guess_year_from_url(url)
                    language = _guess_language(title, url)
                    quality = _guess_quality(title)
                    movies.append({
                        "title": title,
                        "url": url,
                        "year": year,
                        "language": language,
                        "quality": quality,
                    })
            continue

        # Not cached — fetch and scrape
        resp = _get(session, listing_url)
        if not resp or resp.status_code != 200:
            continue

        html = resp.text
        found = {}

        # Strategy 1: Extract from <a title="..." href="movie-url.html">
        for title, url in re.findall(
            r'<a[^>]+title="([^"]+)"[^>]+href="(https?://[^"]+\.html)"',
            html,
        ):
            if MOVIE_URL_RE.match(url) and url not in seen_urls:
                found[url] = title

        # Strategy 2: Extract from <p><b>title</b></p> with nearby <a href="...">
        # Find all .boxed .film divs and extract title + link
        for block in re.findall(
            r'<div class="boxed film">(.*?)</div>\s*</li>', html, re.DOTALL
        ):
            title_match = re.search(r'<p><b>([^<]+)</b></p>', block)
            link_match = re.search(r'<a[^>]+href="(https?://[^"]+\.html)"', block)
            if title_match and link_match:
                url = link_match.group(1)
                title = title_match.group(1)
                if MOVIE_URL_RE.match(url) and url not in seen_urls:
                    found[url] = title

        for url, title in found.items():
            seen_urls.add(url)
            year = _guess_year(title)
            language = _guess_language(title, url)
            quality = _guess_quality(title)
            movies.append({
                "title": title,
                "url": url,
                "year": year,
                "language": language,
                "quality": quality,
            })

        # Cache this page's results
        page_cache[listing_url] = list(found.keys())
        print(f"[scraper]   {listing_url} -> {len(found)} movies (fetched + cached)")
        time.sleep(POLITE_DELAY)

    return movies, page_cache


def _guess_year(title):
    """Extract year from title like 'Movie DVDScr [Telugu]'"""
    match = re.search(r'\((\d{4})\)', title)
    return int(match.group(1)) if match else None


def _guess_language(title, url):
    """Detect language from title brackets or URL."""
    combined = (title + " " + url).lower()
    languages = {
        "telugu": "Telugu",
        "tamil": "Tamil",
        "malayalam": "Malayalam",
        "hindi": "Hindi",
        "kannada": "Kannada",
        "bengali": "Bengali",
        "punjabi": "Punjabi",
        "english": "English",
        "hollywood": "English",
    }
    for key, label in languages.items():
        if key in combined:
            return label
    return "Other"


def _guess_quality(title):
    """Extract quality from title like 'DVDScr', 'HDRip', 'CAM', etc."""
    match = re.search(r'\((\d{4})\)\s+([^\s\[]+)', title)
    if match:
        return match.group(2)
    return ""


# ---------------------------------------------------------------------------
# Step 3: Extract stream URLs from movie pages
# ---------------------------------------------------------------------------

def extract_streams(session, movies):
    """
    For each movie page, extract the raw HLS stream URL.
    """
    results = []
    total = len(movies)

    for idx, movie in enumerate(movies, 1):
        print(f"[extractor] ({idx}/{total}) {movie['title']}")

        resp = _get(session, movie["url"])
        if not resp or resp.status_code != 200:
            print(f"  [skip] could not fetch page")
            continue

        html = resp.text

        # Find embedded player iframes: var locations = ["url1", "url2"];
        # Also check for: var players = [...]
        iframe_urls = []

        # Pattern 1: var locations = [...]
        match = re.search(r'var\s+locations\s*=\s*\[(.*?)\];', resp.text, re.DOTALL)
        if match:
            for raw_url in re.findall(r'"([^"]+)"', match.group(1)):
                iframe_urls.append(raw_url.replace('\\/', '/'))

        # Pattern 2: var players = [...]
        if not iframe_urls:
            match = re.search(r'var\s+players\s*=\s*\[(.*?)\];', resp.text, re.DOTALL)
            if match:
                for raw_url in re.findall(r'"([^"]+)"', match.group(1)):
                    iframe_urls.append(raw_url.replace('\\/', '/'))

        # Pattern 3: var file = "..." (single URL)
        if not iframe_urls:
            match = re.search(r'var\s+file\s*=\s*["\']([^"\']+)["\']', resp.text)
            if match:
                iframe_urls.append(match.group(1))

        if not iframe_urls:
            # Also try to find any iframe/src on the page
            iframe_urls = re.findall(r'(?:iframe|src)\s*[=:]\s*["\']([^"\']*(?:player|embed|stream|video)[^"\']*)["\']', html, re.IGNORECASE)

        if not iframe_urls:
            print(f"  [skip] no player iframes found")
            continue

        stream_url = None
        for iframe_url in iframe_urls:
            stream_url = _extract_from_iframe(session, iframe_url, movie["url"])
            if stream_url:
                break
            time.sleep(POLITE_DELAY)

        if stream_url:
            movie["stream_url"] = stream_url
            movie["added_at"] = datetime.now(timezone.utc).isoformat()
            movie["entry_id"] = _make_entry_id(movie["url"])
            results.append(movie)
            print(f"  [ok] stream found")
        else:
            print(f"  [skip] could not extract stream from any mirror")

    return results


def _extract_from_iframe(session, iframe_url, referer):
    """Try 3 regex strategies on an iframe page to find the HLS URL."""
    try:
        resp = session.get(
            iframe_url,
            headers={"User-Agent": USER_AGENT, "Referer": referer},
            timeout=12,
        )
        if resp.status_code != 200:
            return None

        html = resp.text

        # Strategy 1: <source src="...">
        m = re.search(r'<source[^>]+src=["\']([^"\']+)["\']', html)
        if m:
            url = m.group(1).strip()
            if url.startswith("http"):
                return url

        # Strategy 2: const source = "..."
        m = re.search(r'(?:const|var|let)\s+source\s*=\s*["\']([^"\']+)["\']', html)
        if m:
            url = m.group(1).strip()
            if url.startswith("http"):
                return url

        # Strategy 3: General HLS/VCDN pattern
        m = re.search(r'["\'](https?://[^"\']*(?:vcdn|hls|m3u8)[^"\']*)["\']', html)
        if m:
            return m.group(1).replace('\\/', '/')

    except Exception as exc:
        print(f"    [warn] iframe error: {exc}")

    return None


# ---------------------------------------------------------------------------
# Step 4: Build M3U playlist
# ---------------------------------------------------------------------------

def build_m3u(movies, proxy_template):
    """Generate an IPTV-compatible M3U playlist string."""
    lines = ["#EXTM3U", ""]

    for movie in movies:
        raw_url = movie.get("stream_url", "")
        if not raw_url:
            continue

        proxy_url = proxy_template.replace("{url}", requests.utils.quote(raw_url, safe=""))

        title = movie["title"]
        year = movie.get("year")
        group = _guess_group(movie)
        logo = ""

        display_name = title

        lines.append(
            f'#EXTINF:-1 tvg-name="{title}" tvg-logo="{logo}" '
            f'group-title="{group}",{display_name}'
        )
        lines.append(proxy_url)

    return "\n".join(lines) + "\n"


def _guess_group(movie):
    """Determine a category group from the movie data."""
    combined = (movie["url"] + " " + movie["title"]).lower()

    languages = {
        "telugu": "Telugu",
        "tamil": "Tamil",
        "malayalam": "Malayalam",
        "hindi": "Hindi",
        "kannada": "Kannada",
        "bengali": "Bengali",
        "punjabi": "Punjabi",
        "english": "English",
        "hollywood": "English",
    }
    for key, label in languages.items():
        if key in combined:
            return f"Movies / {label}"

    return "Movies / Other"


# ---------------------------------------------------------------------------
# Page listing cache
# ---------------------------------------------------------------------------

def load_page_cache(cache_file):
    """Load cached page→movie URL mappings from disk."""
    path = Path(cache_file)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_page_cache(cache, cache_file):
    """Persist the page cache to disk."""
    Path(cache_file).write_text(json.dumps(cache, indent=2))


def get_known_page_urls(page_cache):
    """Get set of all movie URLs already seen from page scraping."""
    urls = set()
    for page_url, movie_urls in page_cache.items():
        urls.update(movie_urls)
    return urls


# ---------------------------------------------------------------------------
# Step 5: Deduplication & caching
# ---------------------------------------------------------------------------

def _make_entry_id(url):
    """Generate a stable hash ID from the movie page URL."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def load_cache(cache_file):
    """Load the dedup cache from disk."""
    path = Path(cache_file)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_cache(cache, cache_file):
    """Persist the dedup cache to disk."""
    Path(cache_file).write_text(json.dumps(cache, indent=2))


def deduplicate(movies, cache):
    """Filter out movies whose entry_id is already in the cache."""
    new_movies = []
    for movie in movies:
        eid = movie.get("entry_id")
        if eid and eid in cache:
            continue
        if eid:
            cache[eid] = movie
        new_movies.append(movie)
    return new_movies, cache


def merge_with_existing(new_content, existing_file):
    """
    Append new movie entries to the existing playlist.
    Skips entries whose stream URL already exists in the playlist.
    Preserves all existing entries and their URLs intact.
    """
    existing_path = Path(existing_file)
    if not existing_path.exists() or existing_path.stat().st_size == 0:
        return new_content

    existing = existing_path.read_text()

    # Collect existing stream URLs for dedup
    existing_urls = set()
    lines = existing.split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            existing_urls.add(stripped)

    # Parse new entries into (header, url) pairs
    new_entries = []
    current_header = None
    current_url = None
    for line in new_content.strip().split("\n"):
        stripped = line.strip()
        # Skip the #EXTM3U header from build_m3u since existing has one
        if stripped == "#EXTM3U" or stripped == "":
            continue
        if stripped.startswith("#EXTINF"):
            current_header = stripped
            current_url = None
        elif not stripped.startswith("#"):
            current_url = stripped
            if current_header:
                new_entries.append((current_header, current_url))
                current_header = None
                current_url = None

    # Filter: only keep entries whose URL isn't already present
    unique_new = [
        (header, url)
        for header, url in new_entries
        if url not in existing_urls
    ]

    if not unique_new:
        return existing

    # Build the appended content
    appended_lines = ["", ""]
    for header, url in unique_new:
        appended_lines.append(header)
        appended_lines.append(url)
    appended_lines.append("")

    return existing.rstrip("\n") + "\n" + "\n".join(appended_lines) + "\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(output="playlist.m3u", append=False, cache_file=DEFAULT_CACHE_FILE):
    """
    Run the full pipeline: scrape -> extract -> build M3U.

    This is incremental:
      - Uses page cache to skip already-scraped listing pages
      - Skips movies already in the entry cache
      - Extracts streams for up to MAX_STREAMS_PER_RUN new movies
      - Over multiple runs, the playlist grows to include all movies
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # Load both caches
    cache = load_cache(cache_file)
    page_cache = load_page_cache(DEFAULT_PAGE_CACHE_FILE)

    # Step 1: Discover listing pages (uses cache to skip known pages)
    listing_urls, page_cache = discover_listing_pages(session, MOVIERULZ_DOMAINS[0], page_cache)
    if not listing_urls:
        print("[error] No listing pages found -- is Movierulz reachable?")
        sys.exit(1)

    # Step 2: Collect movie links (uses cache for instant page loads)
    movies, page_cache = extract_movie_links(session, listing_urls, page_cache)
    print(f"[scraper] Found {len(movies)} movies across all pages")

    if not movies:
        print("[info] No movies found -- skipping this run")
        save_page_cache(page_cache, DEFAULT_PAGE_CACHE_FILE)
        return "#EXTM3U\n"

    # Step 3: Filter out already-processed movies using cache
    pending = []
    for movie in movies:
        eid = _make_entry_id(movie["url"])
        if eid not in cache:
            pending.append(movie)

    print(f"[dedup] {len(movies) - len(pending)} already processed, {len(pending)} pending")

    if not pending:
        print("[info] All movies already in playlist -- up to date")
        save_page_cache(page_cache, DEFAULT_PAGE_CACHE_FILE)
        return "#EXTM3U\n"

    # Step 4: Extract streams for a batch of pending movies
    batch = pending[:MAX_STREAMS_PER_RUN]
    print(f"[extractor] Processing batch of {len(batch)} movies (max per run: {MAX_STREAMS_PER_RUN})")

    enriched = extract_streams(session, batch)
    print(f"[extractor] Successfully extracted {len(enriched)} streams")

    if not enriched:
        print("[info] No streams extracted -- skipping this run")
        save_page_cache(page_cache, DEFAULT_PAGE_CACHE_FILE)
        return "#EXTM3U\n"

    # Step 5: Build M3U content
    m3u_content = build_m3u(enriched, PROXY_TEMPLATE)

    # Step 6: Merge with existing playlist if appending
    if append:
        m3u_content = merge_with_existing(m3u_content, output)

    # Step 7: Ensure no duplicate #EXTM3U headers
    lines = m3u_content.split("\n")
    cleaned = []
    extm3u_seen = False
    for line in lines:
        if line.strip() == "#EXTM3U":
            if not extm3u_seen:
                cleaned.append(line)
                extm3u_seen = True
        else:
            cleaned.append(line)
    m3u_content = "\n".join(cleaned)

    # Step 8: Update caches
    for movie in enriched:
        cache[_make_entry_id(movie["url"])] = movie
    enriched_urls = {_make_entry_id(m["url"]) for m in enriched}
    for movie in batch:
        eid = _make_entry_id(movie["url"])
        if eid not in enriched_urls and eid not in cache:
            cache[eid] = {"url": movie["url"], "failed": True}
    save_cache(cache, cache_file)
    save_page_cache(page_cache, DEFAULT_PAGE_CACHE_FILE)

    return m3u_content


def main():
    parser = argparse.ArgumentParser(
        description="Build an IPTV M3U playlist from Movierulz"
    )
    parser.add_argument(
        "--output", "-o", default="playlist.m3u",
        help="Output M3U file path (default: playlist.m3u)",
    )
    parser.add_argument(
        "--append", "-a", action="store_true",
        help="Append new movies to existing playlist instead of overwriting",
    )
    parser.add_argument(
        "--cache", "-c", default=DEFAULT_CACHE_FILE,
        help=f"Cache file for dedup (default: {DEFAULT_CACHE_FILE})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate M3U but don't write to file",
    )
    args = parser.parse_args()

    content = run(output=args.output, append=args.append, cache_file=args.cache)

    if args.dry_run:
        print(content)
    else:
        Path(args.output).write_text(content)
        entry_count = len([l for l in content.split("\n") if l.startswith("#EXTINF")])
        print(f"[done] Wrote {entry_count} entries to {args.output}")


if __name__ == "__main__":
    main()
