# 🎬 Movierulz IPTV Playlist Builder

Automatically builds and updates an IPTV-compatible `.m3u` playlist from Movierulz movie listings. Point your **Android TV**, **iPhone**, or any HLS player at the playlist URL — new movies are added every 6 hours with zero maintenance.

**No server to run. No local proxy. No port forwarding. Everything runs for free on GitHub + Cloudflare.**

---

## How It Works

```
┌──────────────────────────────────────────────────────────────┐
│  GitHub Actions (every 6 hours, free)                        │
│                                                              │
│  1. Scrape Movierulz movie listings                          │
│  2. Extract HLS stream URLs from each movie page             │
│  3. Deduplicate against cache                                │
│  4. Build IPTV M3U playlist with proxy URLs                  │
│  5. Commit to repo → served by GitHub Pages                  │
└──────────────────────────────────────────────────────────────┘
         │
         ▼
  GitHub Pages: https://<user>.github.io/<repo>/playlist.m3u
         │
         ▼
  Your Android TV + OTT Navigator / TiviMate / any IPTV app
         │
         ▼
  Cloudflare Worker (free edge proxy)
  ├── Adds Referer/Origin headers
  ├── Rewrites M3U8 segment URLs
  └── Strips fake PNG headers from .ts chunks
         │
         ▼
  Video plays 🎬
```

---

## Quick Start

### Prerequisites

- A **GitHub account** (free)
- A **Cloudflare account** (free — no credit card needed for Workers free tier)
- **Movierulz** access from your network

### Step 1: Deploy the Cloudflare Worker (2 minutes)

1. Go to **[Cloudflare Dashboard → Workers & Pages](https://dash.cloudflare.com/?to=/:account/workers/create)**
2. Click **"Create Application"** → **"Start with Hello World!"**
3. Name it something like `movierulz-proxy`
4. Delete the default code and paste the contents of [`worker.js`](worker.js)
5. Click **"Deploy"**
6. Copy your worker URL: `https://movierulz-proxy.<your-account>.workers.dev`

### Step 2: Create the GitHub Repo

1. **Fork or create** this repo on GitHub
2. Go to **Settings → Secrets and variables → Actions**
3. Click **"New repository secret"**
4. Add:
   - **Name:** `PROXY_URL`
   - **Value:** `https://movierulz-proxy.<your-account>.workers.dev`
5. Enable **GitHub Pages**:
   - Settings → Pages → Source: **Deploy from a branch** → Branch: **main** → **/root** → Save
   - Your playlist will be at: `https://<user>.github.io/<repo>/playlist.m3u`

### Step 3: Add to Your IPTV App

#### OTT Navigator (Android TV)

1. Open **OTT Navigator** → **Playlists** → **Add playlist**
2. Select **"M3U URL"**
3. Paste your GitHub Pages URL:
   ```
   https://<user>.github.io/<repo>/playlist.m3u
   ```
4. Name it (e.g. "Movierulz Movies")
5. Set **Refresh period** to 6 hours (matches the GitHub Actions schedule)
6. Save and wait for the playlist to load

#### TiviMate (Android TV)

1. Open **TiviMate** → **Playlists** → **Add playlist**
2. Select **"M3U URL"**
3. Paste the same GitHub Pages URL
4. Set **EPG source** to "None" (we don't have an EPG)
5. Save

#### VLC (Desktop / Mobile)

1. Open VLC → **Media → Open Network Stream**
2. Paste the playlist URL
3. Or add the URL to **VLC Playlists** for persistent access

---

## Files

| File | Purpose |
|------|---------|
| [`playlist_builder.py`](playlist_builder.py) | Scrapes Movierulz, extracts streams, builds M3U with IPTV metadata |
| [`worker.js`](worker.js) | Cloudflare Worker proxy — add headers, rewrite URLs, strip PNG obfuscation |
| [`.github/workflows/update.yml`](.github/workflows/update.yml) | GitHub Actions cron — runs every 6 hours |
| [`playlist.m3u`](playlist.m3u) | The output playlist (served by GitHub Pages) |
| [`requirements.txt`](requirements.txt) | Python dependencies |
| [`.gitignore`](.gitignore) | Ignores generated files |
| [`DEPLOY.md`](DEPLOY.md) | Detailed deployment guide |

---

## Playlist Format

Each entry in the playlist looks like:

```
#EXTINF:-1 tvg-name="Movie Title" tvg-logo="" group-title="Movies / Tamil",Movie Title
https://<worker-url>/proxy?url=https%3A%2F%2Fhls2.vcdnx.com%2F...
```

| Field | Description |
|-------|-------------|
| `tvg-name` | Movie title (used for search in IPTV apps) |
| `tvg-logo` | Poster/logo URL (empty — can be enhanced with TMDB later) |
| `group-title` | Category folder — `Movies / Tamil`, `Movies / Hindi`, etc. |
| URL | Proxy URL that goes through the Cloudflare Worker |

---

## Manual Trigger

You can trigger a playlist update anytime:

1. Go to your repo on GitHub
2. Click **Actions** tab
3. Select **"Update Playlist"** workflow
4. Click **"Run workflow"** → **"Run workflow"** (green button)

---

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PROXY_URL` | *(required secret)* | Your Cloudflare Worker base URL |
| `MAX_LIST_PAGES` | `10` | How many listing pages to scrape per run |
| `POLITE_DELAY` | `1.0` | Seconds between iframe fetches (avoid rate-limiting) |

### Local Testing

```bash
# Install dependencies
pip install -r requirements.txt

# Set your proxy URL
export PROXY_TEMPLATE="https://my-worker.workers.dev/proxy?url={url}"

# Build playlist (overwrites)
python playlist_builder.py --output playlist.m3u

# Append to existing playlist
python playlist_builder.py --output playlist.m3u --append

# Preview without writing
python playlist_builder.py --dry-run
```

---

## Limits & Notes

| Limit | Detail |
|-------|--------|
| **Cloudflare Workers free tier** | 100,000 requests/day. Each movie play = ~100-1000 requests (playlist + segments). A 50-movie playlist playing on 3 devices = ~15k requests/hour. Should be fine for personal use. |
| **GitHub Actions** | 2,000 minutes/month free. Each run takes ~5-10 min (depends on movies scraped). |
| **Stream availability** | Movierulz links can go dead. The playlist auto-updates, but individual entries may stop working. IPTV apps handle this gracefully. |
| **Rate limiting** | The script includes polite delays between requests. Don't lower `POLITE_DELAY` below 0.5s. |
| **Region blocking** | If Movierulz is blocked in your region, the GitHub Actions runner (US-based) may still access it. |
| **Proxy cost** | Cloudflare Workers free tier is generous. If you exceed it (~100 movies × frequent viewing), the paid tier is $5/month. |

---

## Troubleshooting

### Playlist shows "Loading..." forever
- Check the GitHub Actions run log for errors
- Make sure `PROXY_URL` secret is set correctly
- Verify the Cloudflare Worker is deployed and responding

### "403 Forbidden" on specific movies
- The iframe mirror might be dead. The playlist builder tries all available mirrors, but some may fail
- Trigger a manual workflow run — next build might get a working mirror

### No new movies appearing
- Movierulz may have changed their page structure. Check the GitHub Actions logs for errors
- Increase `MAX_LIST_PAGES` to scrape more pages

### OTT Navigator shows blank screen
- Make sure the playlist URL uses HTTPS
- Try reducing the playlist size — some apps struggle with very large playlists on first load
- In OTT Navigator, go to Settings → Player → "External player" and select VLC or MX Player

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Credits

Built on the extraction logic from [Movierulz Video Extractor](https://github.com). Proxy concept inspired by the same project.
