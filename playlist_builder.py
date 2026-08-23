"""
playlist_builder.py — Movierulz → IPTV Playlist Builder
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

# Proxy URL template — the Cloudflare Worker proxy.
# {url} will be URL-encoded stream URL.
PROXY_TEMPLATE = os.environ.get(
    "PROXY_TEMPLATE",
    "https://my-worker.workers.dev/proxy?url={url}",
)

# How many pages of the movie listing to scrape per run.
MAX_LIST_PAGES = int(os.environ.get("MAX_LIST_PAGES", "10"))

# Seconds to wait between iframe fetches to avoid rate-limiting.
POLITE_DELAY = float(os.environ.get("POLITE_DELAY", "1.0"))

# Movierulz domains to try (the site changes TLDs frequently).
MOVIERULZ_DOMAINS = [
    "https://www.5movierulz.watch",
    "https://www.5movierulz.viajes",
    "https://www.5movierulz.cfd",
    "https://www.5movierulz.green",
    "https://www.5movierulz.lat",
]

# Cache file for dedup (stored alongside the playlist).
DEFAULT_CACHE_FILE = ".playlist_cache.json"


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

def discover_listing_pages(session, base_url):
    """
    Find paginated movie listing URLs from the Movierulz homepage.
    Returns a list of absolute page URLs to scrape for movie links.
    """
    for domain in MOVIERULZ_DOMAINS:
        resp = _get(session, domain)
        if not resp or resp.status_code != 200:
            continue

        html = resp.text

        # Strategy A: Find pagination links (e.g. /page/2/, /page/3/)
        page_links = set(re.findall(r'href=["\']([^"\']*\/page\/\d+[^"\']*)["\']', html))
        listing_urls = []

        if page_links:
            for link in page_links:
                listing_urls.append(urljoin(domain, link))
        else:
            # Strategy B: No pagination — homepage is the listing
            listing_urls = [domain]

        # Deduplicate and limit
        seen = set()
        unique = []
        for u in listing_urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)
        listing_urls = unique[:MAX_LIST_PAGES]

        print(f"[scraper] Found {len(listing_urls)} listing page(s) on {domain}")
        return listing_urls

    print("[scraper] Could not reach any Movierulz domain")
    return []


# ---------------------------------------------------------------------------
# Step 2: Extract movie page links from listing pages
# ---------------------------------------------------------------------------

def extract_movie_links(session, listing_urls):
    """
    Scrape each listing page and collect movie entries with:
      - title (str)
      - url   (str)
      - year  (int, extracted from URL/title)
    """
    movies = []
    seen_urls = set()

    for listing_url in listing_urls:
        resp = _get(session, listing_url)
        if not resp or resp.status_code != 200:
            continue

        html = resp.text
        link_patterns = [
            r'href=["\']([^"\']*\/movie[^"\']*)["\']',
            r'href=["\']([^"\']*movie-watch-online[^"\']*)["\']',
        ]
        found = set()
        for pattern in link_patterns:
            for match in re.findall(pattern, html):
                abs_url = urljoin(listing_url, match)
                if abs_url not in seen_urls:
                    found.add(abs_url)

        for url in found:
            seen_urls.add(url)
            title = _guess_title_from_url(url)
            year = _guess_year_from_url(url)
            movies.append({"title": title, "url": url, "year": year})

        print(f"[scraper]   {listing_url} -> {len(found)} movies")
        time.sleep(POLITE_DELAY)

    return movies


def _guess_title_from_url(url):
    """Extract a readable title from the URL slug."""
    parts = url.split("/")
    slug = parts[-2] if len(parts) >= 2 else url
    slug = re.sub(
        r'-(online|free|watch|movie|hindi|tamil|telugu|malayalam|kannada)$',
        '', slug, flags=re.IGNORECASE,
    )
    slug = re.sub(r'-\d{4}', '', slug)
    return re.sub(r'[-_]+', ' ', slug).strip().title()


def _guess_year_from_url(url):
    """Try to extract a 4-digit year from the URL."""
    match = re.search(r'/(20\d{2})', url)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Step 3: Extract stream URLs from movie pages
# ---------------------------------------------------------------------------

def extract_streams(session, movies):
    """
    For each movie page, extract the raw HLS stream URL.
    Uses the same 3-strategy regex approach from url_extractor.py.
    Returns enriched movie dicts with a 'stream_url' key.
    """
    results = []
    total = len(movies)

    for idx, movie in enumerate(movies, 1):
        print(f"[extractor] ({idx}/{total}) {movie['title']}")

        resp = _get(session, movie["url"])
        if not resp or resp.status_code != 200:
            print(f"  [skip] could not fetch page")
            continue

        # Find embedded player iframes: var locations = ["url1", "url2"];
        iframe_urls = []
        match = re.search(r'var locations\s*=\s*\[(.*?)\];', resp.text)
        if match:
            for raw_url in re.findall(r'"([^"]+)"', match.group(1)):
                iframe_urls.append(raw_url.replace('\\/', '/'))

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

        # Build proxy URL for the stream
        proxy_url = proxy_template.replace("{url}", requests.utils.quote(raw_url, safe=""))

        # Build M3U metadata
        title = movie["title"]
        year = movie.get("year")
        group = _guess_group(movie)
        logo = ""

        year_str = f" ({year})" if year else ""
        display_name = f"{title}{year_str}"

        lines.append(
            f'#EXTINF:-1 tvg-name="{title}" tvg-logo="{logo}" '
            f'group-title="{group}",{display_name}'
        )
        lines.append(proxy_url)

    return "\n".join(lines) + "\n"


def _guess_group(movie):
    """Determine a category group from the movie data."""
    url_lower = movie["url"].lower()
    title_lower = movie["title"].lower()

    languages = {
        "hindi": "Hindi",
        "tamil": "Tamil",
        "telugu": "Telugu",
        "malayalam": "Malayalam",
        "kannada": "Kannada",
        "bengali": "Bengali",
        "punjabi": "Punjabi",
        "english": "English",
    }
    for key, label in languages.items():
        if key in url_lower or key in title_lower:
            return f"Movies / {label}"

    return "Movies / Other"


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
    Merge newly extracted movies into the existing playlist.
    New entries are appended. Duplicate detection done beforehand.
    """
    existing_path = Path(existing_file)
    if existing_path.exists() and existing_path.stat().st_size > 0:
        existing = existing_path.read_text()
        lines = existing.split("\n")

        # Keep header (up to first blank line after #EXTM3U)
        header_end = 0
        for i, line in enumerate(lines):
            if line.strip() == "":
                header_end = i + 1
                break
        header = "\n".join(lines[:header_end])

        # Collect existing segment URLs for dedup
        existing_urls = set()
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                existing_urls.add(line)

        # Filter new entries against existing URLs
        new_lines = new_content.strip().split("\n")
        filtered = []
        skip = False
        for line in new_lines:
            if line.startswith("#EXTINF"):
                skip = False
                filtered.append(line)
            elif line and not line.startswith("#"):
                if line not in existing_urls:
                    filtered.append(line)
                else:
                    skip = True
            elif not skip:
                filtered.append(line)

        return header + "\n" + "\n".join(filtered) + "\n"

    return new_content


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(output="playlist.m3u", append=False, cache_file=DEFAULT_CACHE_FILE):
    """
    Run the full pipeline: scrape -> extract -> build M3U.

    Returns the generated M3U content.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    # Step 1: Find listing pages
    listing_urls = discover_listing_pages(session, MOVIERULZ_DOMAINS[0])
    if not listing_urls:
        print("[error] No listing pages found -- is Movierulz reachable?")
        sys.exit(1)

    # Step 2: Collect movie links
    movies = extract_movie_links(session, listing_urls)
    print(f"[scraper] Found {len(movies)} movies across all pages")

    if not movies:
        print("[info] No movies found -- skipping this run")
        return "#EXTM3U\n"

    # Step 3: Extract stream URLs
    enriched = extract_streams(session, movies)
    print(f"[extractor] Successfully extracted {len(enriched)} streams")

    if not enriched:
        print("[info] No streams extracted -- skipping this run")
        return "#EXTM3U\n"

    # Step 4: Deduplicate against cache
    cache = load_cache(cache_file)
    new_movies, cache = deduplicate(enriched, cache)
    print(
        f"[dedup] {len(new_movies)} new movies "
        f"(skipped {len(enriched) - len(new_movies)} duplicates)"
    )

    if not new_movies:
        print("[info] No new movies to add -- playlist is up to date")
        return "#EXTM3U\n"

    # Step 5: Build M3U content
    m3u_content = build_m3u(new_movies, PROXY_TEMPLATE)

    # Step 6: Merge with existing playlist if appending
    if append:
        m3u_content = merge_with_existing(m3u_content, output)

    # Step 7: Save cache
    save_cache(cache, cache_file)

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
