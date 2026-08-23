/**
 * Cloudflare Worker — Movierulz HLS Stream Proxy
 * ==============================================
 * Transparent proxy for HLS video streams. Solves three problems:
 *
 *   1. Adds required Referer + Origin headers (video host blocks bare requests)
 *   2. Rewrites M3U8 segment URLs to route through this worker
 *   3. Strips fake PNG headers obfuscating .ts video chunks
 *
 * Deploy:
 *   1. Go to https://dash.cloudflare.com/?to=/:account/workers/create
 *   2. Name it (e.g. "movierulz-proxy")
 *   3. Paste this file into the editor
 *   4. Save & Deploy
 *   5. Your worker URL: https://<name>.<account>.workers.dev
 *   6. Set that URL as the PROXY_URL secret in your GitHub repo
 */

const STREAM_HEADERS = {
  "User-Agent":
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    + "AppleWebKit/537.36 (KHTML, like Gecko) "
    + "Chrome/120.0.0.0 Safari/537.36",
  "Referer": "https://ww7.vcdnlare.com/",
  "Origin": "https://ww7.vcdnlare.com",
};

// MPEG-TS sync byte — appears every 188 bytes in valid TS packets
const TS_SYNC = 0x47;

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const target = url.searchParams.get("url");

    if (!target) {
      return new Response(JSON.stringify({ error: "Missing ?url= parameter" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    try {
      // Fetch the upstream resource with required headers
      const upstream = await fetch(target, {
        headers: STREAM_HEADERS,
      });

      const contentType = upstream.headers.get("content-type") || "";
      const body = await upstream.clone().arrayBuffer();
      const bytes = new Uint8Array(body);

      // ---------------------------------------------------------------
      // Case 1: M3U8 Playlist — rewrite all segment URLs through proxy
      // ---------------------------------------------------------------
      if (isM3U8(contentType, bytes)) {
        const text = new TextDecoder().decode(bytes);
        const rewritten = rewriteM3U8(text, target, request.url);
        return new Response(rewritten, {
          headers: {
            "Content-Type": "application/vnd.apple.mpegurl",
            "Access-Control-Allow-Origin": "*",
            "Cache-Control": "no-cache",
          },
        });
      }

      // ---------------------------------------------------------------
      // Case 2: .ts chunk with fake PNG header — strip it
      // ---------------------------------------------------------------
      if (isPngWrapped(bytes)) {
        const stripped = stripPngHeader(bytes);
        if (stripped) {
          return new Response(stripped, {
            headers: {
              "Content-Type": "video/MP2T",
              "Access-Control-Allow-Origin": "*",
              "Cache-Control": "no-cache",
            },
          });
        }
      }

      // ---------------------------------------------------------------
      // Case 3: Pass through (AES key, etc.)
      // ---------------------------------------------------------------
      return new Response(body, {
        headers: {
          "Content-Type": contentType || "application/octet-stream",
          "Access-Control-Allow-Origin": "*",
          "Cache-Control": "no-cache",
        },
      });

    } catch (err) {
      return new Response(
        JSON.stringify({ error: `Proxy error: ${err.message}` }),
        {
          status: 502,
          headers: { "Content-Type": "application/json" },
        },
      );
    }
  },
};

// ---------------------------------------------------------------------------
// Detection helpers
// ---------------------------------------------------------------------------

function isM3U8(contentType, bytes) {
  if (contentType.includes("application/vnd.apple.mpegurl")) return true;
  if (new TextDecoder().decode(bytes.slice(0, 7)) === "#EXTM3U") return true;
  return false;
}

function isPngWrapped(bytes) {
  // PNG magic bytes: \x89PNG\r\n\x1a\n
  return (
    bytes.length > 69 &&
    bytes[0] === 0x89 &&
    bytes[1] === 0x50 && // P
    bytes[2] === 0x4E && // N
    bytes[3] === 0x47 && // G
    bytes[4] === 0x0D &&
    bytes[5] === 0x0A &&
    bytes[6] === 0x1A &&
    bytes[7] === 0x0A
  );
}

// ---------------------------------------------------------------------------
// M3U8 URL rewriting
// ---------------------------------------------------------------------------

function rewriteM3U8(text, baseUrl, workerUrl) {
  const proxyBase = workerUrl.split("?")[0];
  const lines = text.split("\n");
  const result = [];

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed) {
      result.push(line);
      continue;
    }

    if (trimmed.startsWith("#")) {
      // Rewrite URI attributes in tags like #EXT-X-KEY:URI="..."
      const rewritten = trimmed.replace(
        /URI="([^"]+)"/g,
        (match, uri) =>
          `URI="${proxyBase}?url=${encodeURIComponent(resolveUrl(baseUrl, uri))}"`,
      );
      result.push(rewritten);
    } else {
      // It's a segment or playlist URL — rewrite it
      const absolute = resolveUrl(baseUrl, trimmed);
      result.push(`${proxyBase}?url=${encodeURIComponent(absolute)}`);
    }
  }

  return result.join("\n");
}

function resolveUrl(base, relative) {
  try {
    return new URL(relative, base).href;
  } catch {
    return relative;
  }
}

// ---------------------------------------------------------------------------
// PNG header stripping
// ---------------------------------------------------------------------------

function stripPngHeader(bytes) {
  const len = bytes.length;

  // Strategy 1: Strong 3-sync check (most reliable)
  const limit3 = Math.max(0, len - 376);
  for (let i = 0; i < limit3; i++) {
    if (
      bytes[i] === TS_SYNC &&
      bytes[i + 188] === TS_SYNC &&
      bytes[i + 376] === TS_SYNC
    ) {
      return bytes.slice(i);
    }
  }

  // Strategy 2: Fallback — 2-sync check (for shorter content)
  const limit2 = Math.max(0, len - 188);
  for (let i = 0; i < limit2; i++) {
    if (bytes[i] === TS_SYNC && bytes[i + 188] === TS_SYNC) {
      return bytes.slice(i);
    }
  }

  // No TS sync found — might be an AES key or other non-TS data
  return null;
}
