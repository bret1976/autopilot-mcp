# 6Frame Autopilot MCP

A sellable MCP: buyers paste one private URL into Claude, Grok, Codex, Cursor, Google Antigravity, ChatGPT, or Claude Code. They enter **their** Gemini and PostProxy keys. Autopilot runs the locked spine:

scan a viral AI-filmmaking original → download it → trim under 60s → write 6Frame-voice copy with hashtags → PostProxy publishes.

9:16 for Instagram, TikTok, YouTube Shorts, Facebook.  
16:9 for LinkedIn and X.  
YouTube titles include `#Shorts`.

Buyers work inside the LLM they already pay for. They paste one URL. They do not learn a 6Frame app.

**Price: `$997` once** — set in `app/config.py` as `PRICE_USD`. Landing, `/buy`, and `/admin` all read that constant.

Owner: Bret Jenny / 6Frame Studio.

## What a buyer does

1. Fill name + email on `/buy`. The form mints a signed URL (365 days) and shows it immediately. Stripe is optional later.
2. Paste the URL into the model they already pay for.
3. In Claude they say they want TrendPilot / Autopilot for their brand and paste their website. The connector calls `onboard` and **asks for their Gemini key, PostProxy key, profile group, and brand/site before any scan or post**.
4. `setup` saves those keys (never echoed). `set_brand_from_website` pulls voice from their site. Defaults: LinkedIn, X, Instagram, YouTube, Facebook. Daily run 8:00 AM PT.
5. `postproxy_connect` returns OAuth links for **their** socials.
6. `run_autopilot` runs live (`draft=true` first). Mock mode is rejected. No 6Frame stub clips.

The minted URL is path-based so hosts that strip `?token=` (Grok custom connectors) still send the license:

`https://YOUR-HOST/mcp/t/SIGNED_TOKEN`

`/mcp?token=SIGNED_TOKEN` still works. Unauthenticated `/mcp` returns `401` with `WWW-Authenticate` and OAuth discovery so Grok/Claude/ChatGPT can complete a license sign-in instead of dying on “Connection failed.”

### Claude.ai / Grok / ChatGPT

Settings → Connectors → Add custom connector (Grok: grok.com/connectors → New Connector → Custom) → paste the path URL. If the host asks you to sign in, paste the same URL on the connect page.

### Claude Code / Cursor / Codex `mcp.json`

```json
{
  "mcpServers": {
    "6frame-autopilot": {
      "url": "https://YOUR-HOST/mcp/t/SIGNED_TOKEN",
      "headers": {
        "Authorization": "Bearer SIGNED_TOKEN"
      }
    }
  }
}
```

### Google Antigravity

Antigravity uses `serverUrl` (not `url`) in `~/.gemini/antigravity/mcp_config.json`:

```json
{
  "mcpServers": {
    "6frame-autopilot": {
      "serverUrl": "https://YOUR-HOST/mcp/t/SIGNED_TOKEN",
      "headers": {
        "Authorization": "Bearer SIGNED_TOKEN"
      }
    }
  }
}
```

## Tools

| Tool | Role |
| --- | --- |
| `onboard` / `start` | First call. Lists every missing key/brand question. |
| `setup` / `configure` | Save **their** keys and brand. Secrets are never echoed. |
| `set_brand_from_website` | Pull voice from the buyer's site. |
| `postproxy_status` / `postproxy_connect` | Buyer’s PostProxy socials. |
| `scan_trends` | Gemini-grounded scan of a viral original. |
| `download_original` | yt-dlp + ffmpeg trim, 9:16 and 16:9 masters. |
| `write_copy` | Voice + guaranteed hashtags. |
| `publish` | PostProxy, split by aspect. |
| `run_autopilot` | Full spine. `mock=true` is for tests only. |
| `status` / `last_run` | Config (masked) and last run. |

Hashtag caps: LinkedIn/Facebook 5–8, IG/TikTok/YouTube 8–12, X 1–2 on the last tweet under 280. Locked tags: `#6FrameStudio` `#AIFilmmaking` `#AICinema`.

## Env

| Name | Purpose |
| --- | --- |
| `MCP_ISSUER_SECRET` | HMAC secret for buyer tokens. Required in production. |
| `PUBLIC_BASE_URL` | Public origin, no trailing slash. Used when minting URLs and signing media. |
| `ADMIN_SECRET` | Password for `/admin` seller desk. |
| `REVOKED_KEYS` | Comma-separated buyer ids or full tokens to reject at `/mcp`. |
| `DATA_DIR` | Buyer JSON, orders, media. Mount a volume. |
| `PORT` | Default 8080. |
| `STRIPE_PAYMENT_LINK` | Optional. `/buy` still does not collect a card. |

Do not invent API keys. Do not set a global mock mode.

## Mint a buyer URL

```bash
MCP_ISSUER_SECRET=… PUBLIC_BASE_URL=https://your-host \
  python -m app.mint --email buyer@studio.com --buyer-id north-light
```

Or open `/admin`, enter `ADMIN_SECRET`, set buyer + days valid, Generate link. `/buy` already mints and displays the URL. Unauthenticated `/mcp` and `/mcp/` return `401 Missing license key` over HTTPS with no redirect to `http://`. JSON-only `Accept` headers (Grok) get JSON, not a 406.

## Run locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export MCP_ISSUER_SECRET=dev-secret
export ADMIN_SECRET=dev-admin
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

`GET /` landing · `GET /health` · `POST /mcp` · `POST /api/leads` · `/buy` · `/admin`

```bash
pytest
```

## Railway

1. New project → deploy this GitHub repo (`VegasCryptoAgent/autopilot-mcp`), branch `main`.
2. Builder: Dockerfile (see `railway.toml`).
3. Set `MCP_ISSUER_SECRET`, `ADMIN_SECRET`, `PUBLIC_BASE_URL` (the `*.up.railway.app` origin after the first domain is issued).
4. Attach a volume to `DATA_DIR` (e.g. `/data`) so buyer configs survive deploys.
5. Generate a public domain. Health check: `/health`.

Do not put Gemini or PostProxy keys in Railway. Buyers enter those through `setup`.

## Video

Hero video intercuts value cards with live Autopilot footage and a spoken walkthrough. 45–75s. Render with:

```bash
python3 scripts/render_explainer.py
```

Outputs `public/promo.mp4` and `public/poster.jpg`. The YouTube theory clip is a footnote on the landing, not the hero.
