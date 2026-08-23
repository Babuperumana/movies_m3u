# DEPLOY.md — Step-by-Step Deployment Guide

## Part 1: Deploy the Cloudflare Worker Proxy

The proxy is the most critical piece — it makes streams play on devices that can't set HTTP headers.

### 1.1 Create a Cloudflare Account

1. Go to [cloudflare.com](https://dash.cloudflare.com/sign-up)
2. Sign up (free — no credit card required)
3. Verify your email

### 1.2 Deploy the Worker

**Option A: Dashboard (easiest)**

1. Go to **[Workers & Pages](https://dash.cloudflare.com/?to=/:account/workers/create)**
2. Click **"Create Application"** → **"Workers"**
3. Click **"Start with Hello World!"**
4. Name your worker (e.g. `movierulz-proxy`) — this becomes part of the URL
5. In the code editor, **delete everything** and paste the contents of [`worker.js`](worker.js)
6. Click **"Deploy"**
7. After deployment, your URL will be shown: `https://movierulz-proxy.<your-subdomain>.workers.dev`
8. **Copy this URL** — you'll need it for Step 2

**Option B: Wrangler CLI (advanced)**

```bash
npm install -g wrangler
wrangler login
wrangler init movierulz-proxy --type javascript
# Replace the generated index.js with worker.js
wrangler deploy
```

### 1.3 Verify the Worker Works

Test it with a curl command (after you have a stream URL from Step 2):

```bash
curl "https://movierulz-proxy.<your-subdomain>.workers.dev/proxy?url=https%3A%2F%2Fhls2.vcdnx.com%2Fhls%2F..."
```

You should get back an M3U8 playlist (starting with `#EXTM3U`).

---

## Part 2: Set Up the GitHub Repo

### 2.1 Create the Repo

1. Create a new public repo on GitHub (e.g. `m3u-playlist`)
2. Push all the files from this project to the repo
3. Or simply fork this repo if it's public

### 2.2 Enable GitHub Pages

1. Go to **Settings → Pages**
2. Under **Build and deployment → Source**, select **"Deploy from a branch"**
3. Under **Branch**, select **"main"** and **"/root (/)"
4. Click **Save**
5. Your playlist will be served at: `https://<your-username>.github.io/<repo-name>/playlist.m3u`

### 2.3 Add the Proxy URL Secret

1. Go to **Settings → Secrets and variables → Actions**
2. Click **"New repository secret"**
3. **Name:** `PROXY_URL`
4. **Value:** Your Cloudflare Worker URL (from Step 1.2), **without** the `/proxy?url=...` part
   ```
   https://movierulz-proxy.<your-subdomain>.workers.dev
   ```
5. Click **"Add secret"**

### 2.4 Trigger the First Build

1. Go to the **Actions** tab
2. Select **"Update Playlist"** workflow
3. Click **"Run workflow"** → **"Run workflow"** (green button)
4. Wait for the run to complete (5-10 minutes)
5. Once done, `playlist.m3u` will appear in your repo root, and be served by GitHub Pages

### 2.5 Verify

Visit your GitHub Pages URL in a browser:

```
https://<your-username>.github.io/<repo-name>/playlist.m3u
```

You should see a text file with `#EXTM3U` header and `#EXTINF` entries with movie names.

---

## Part 3: Add to Android TV / IPTV App

### 3.1 OTT Navigator

1. Install **OTT Navigator** from Google Play Store on your Android TV / Fire Stick
2. Open the app
3. Go to **Playlists** → **+** (Add playlist)
4. Select **"M3U URL"**
5. Enter your playlist URL:
   ```
   https://<your-username>.github.io/<repo-name>/playlist.m3u
   ```
6. Give it a name (e.g. "Movierulz Movies")
7. **Settings:**
   - **Refresh period:** 6 hours (matches auto-update schedule)
   - **EPG source:** None
   - **Catchup:** Disable
8. Click **OK** / **Save**
9. The app will fetch the playlist — wait a minute for it to load

### 3.2 TiviMate

1. Install **TiviMate** from Play Store
2. Open → **Playlists** → **Add playlist**
3. Select **"M3U URL"**
4. Paste your GitHub Pages URL
5. Name: "Movierulz Movies"
6. Click **Add**
7. Browse the Movies category and play!

### 3.3 VLC (Testing)

1. Open **VLC** on any device
2. **Media → Open Network Stream**
3. Paste the playlist URL
4. Click **Play**
5. VLC will list all available streams

---

## Part 4: Ongoing Maintenance (None Required)

| Task | Frequency | Action |
|------|-----------|--------|
| Playlist updates | Every 6 hours | Automatic via GitHub Actions |
| Worker maintenance | As needed | Only if Cloudflare changes their API |
| Dead stream links | As noticed | Trigger a manual workflow to rebuild |

### Manual Rebuild

If you want to force a fresh scrape (e.g. to clean up dead links):

1. Go to **Actions → Update Playlist → Run workflow**
2. Delete the `.playlist_cache.json` file from the repo first to force re-scanning everything

---

## Troubleshooting

### Worker returns 403 / blocked
- The video host may be blocking the Worker's IP. Try changing the `Referer` in `worker.js`
- Or wait — the site's blocking is often temporary

### GitHub Actions fails with "Could not reach any Movierulz domain"
- Movierulz may have changed domains. Update the `MOVIERULZ_DOMAINS` list in `playlist_builder.py`
- GitHub Actions runners are US-based — if Movierulz is geo-blocked for the US, this won't work

### Playlist loads but streams don't play
- Verify the Cloudflare Worker URL in your GitHub Secret is correct
- Test a stream URL directly: try opening it in a browser with the proxy URL
- Some streams may have expired — trigger a manual rebuild

### Android TV can't load the playlist
- Make sure your Android TV can reach `raw.githubusercontent.com` or your GitHub Pages URL
- If you're behind a restrictive network, you may need a DNS like `1.1.1.1`
- Test the playlist URL in the Android TV's built-in browser first

---

## Cost Summary

| Service | Free Tier | Expected Usage | Cost |
|---------|-----------|----------------|------|
| **GitHub Actions** | 2,000 min/month | ~60 min/month (12 runs) | **$0** |
| **GitHub Pages** | 100 GB/month | <1 GB/month | **$0** |
| **Cloudflare Workers** | 100k requests/day | ~50k/month (personal use) | **$0** |
| **Total** | | | **$0/month** |
