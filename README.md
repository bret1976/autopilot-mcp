# 6Frame Autopilot MCP

A sellable MCP: buyers paste one private URL into Claude, Grok, Codex, Cursor, Google Antigravity, ChatGPT, or Claude Code. They enter **their** Gemini and PostProxy keys. Autopilot runs the locked spine:

scan a viral AI-filmmaking original → download it → trim under 60s → write 6Frame-voice copy with hashtags → PostProxy publishes.

9:16 for Instagram, TikTok, YouTube Shorts, Facebook.  
16:9 for LinkedIn and X.  
YouTube titles include `#Shorts`.

Buyers work inside the LLM they already pay for. They paste one URL. They do not learn a 6Frame app.

**Price: `$997` once** — set in `app/config.py` as `PRICE_USD`. Landing, `/buy`, and `/admin` all read that constant.

Owner: Bret Jenny / 6Frame Studio.

## Agent buyers

Machine discovery (no buy-UI change):

- `/llms.txt`, `/api/pricing`, `/openapi.json`, `/.well-known/mcp/server-card.json`, `/.well-known/agent.json`, `/agent-terms.md`
- `POST /api/orders` → HTTP **402** + `checkout_url` + `checkout_redirect_url` + `session_id` when `STRIPE_SECRET_KEY` is set
- `GET /api/orders/checkout?session_id=cs_test_...` → 302 to the full Stripe Checkout URL (fragment-safe)
- `GET /api/orders/status?session_id=cs_...` → `mcp_url` after paid
- Price stays **$997 once** (`PRICE_USD` in `app/config.py`)


## What a buyer does

1. Fill name + email on `/buy` (or `POST /api/orders` as an agent). Stripe Checkout collects **$997 once**; the license MCP URL is minted only after paid Checkout (webhook or `/buy/thanks` / `GET /api/orders/status`). Admin secret can mint for Bret-mediated sales.
2. Paste the returned MCP URL into the model they already pay for.
3. In Claude they say they want TrendPilot / Autopilot for their brand and paste their website. The connector calls `onboard` and **asks for their Gemini key, PostProxy key, profile group, and brand/site before any scan or post**.
4. `setup` saves those keys (never echoed). `set_brand_from_website` pulls voice from their site. Defaults: LinkedIn, X, Instagram, YouTube, Facebook.
5. `postproxy_connect` returns OAuth links for **their** socials.
6. `run_autopilot` runs live (`draft=true` first). Mock mode is rejected. No 6Frame stub clips.
7. `set_automation` turns on their own daily job (default 8:00 AM PT). They choose `require_approval=true` (stage a draft, then `approve_and_publish`) or `require_approval=false` (scan, download the original, post with no click). The MCP URL does not change. Daily automation stays off until they enable it.

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
| `set_automation` | Enable/disable this buyer's daily scan → download → post. Approval on or off. |
| `approve_and_publish` | Post the last staged draft when `require_approval` is on. |
| `status` / `last_run` | Config (masked), automation schedule, and last run. |

Hashtag caps: LinkedIn/Facebook 5–8, IG/TikTok/YouTube 8–12, X 1–2 on the last tweet under 280. Locked tags: `#6FrameStudio` `#AIFilmmaking` `#AICinema`.

## Env

| Name | Purpose |
| --- | --- |
| `MCP_ISSUER_SECRET` | HMAC secret for buyer tokens. Required in production. |
| `PUBLIC_BASE_URL` | Public origin, no trailing slash. Used when minting URLs and signing media. |
| `ADMIN_SECRET` | Password for `/admin` seller desk. |
| `REVOKED_KEYS` | Comma-separated buyer ids or full tokens to reject at `/mcp`. |
| `YTDLP_COOKIES_FILE` or `YTDLP_COOKIES_B64` | Optional YouTube cookies so Railway can pull source clips YouTube bot-walls. |
| `DATA_DIR` | Buyer JSON, orders, media. Must be a Railway volume (production uses `/data`). Without a volume every deploy wipes Gemini/PostProxy keys. |
| `PORT` | Default 8080. |
| `STRIPE_PAYMENT_LINK` | Optional. `/buy` still does not collect a card. |

Do not invent API keys. Do not set a global mock mode.

## Mint a buyer URL

```bash
MCP_ISSUER_SECRET=… PUBLIC_BASE_URL=https://your-host \
  python -m app.mint --email buyer@studio.com --buyer-id north-light
```

Or open `/admin`, enter `ADMIN_SECRET`, set buyer + days valid, Generate link. `/buy` already mints and displays the URL. Unauthenticated `/mcp` and `/mcp/` return `401 Missing license key` over HTTPS with no redirect to `http://`. JSON-only `Accept` headers (Grok) get JSON, not a 406. A GET that asks for `text/event-stream` gets a keep-alive SSE stream (Claude/Codex/SDK). Hosts that send `notifications/initialized` with an `id` get an empty result instead of `-32602`. Long tools (`scan_trends`, `download_original`, `write_copy`, `publish`, `run_autopilot`) return immediately with `started=true` — the host must poll `status` until `job.status` is `ok` or `error`.

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

1. New project → deploy this GitHub repo (`bret1976/autopilot-mcp`), branch `main`.
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

## Order Guard (order-guard-v1)

Backend-only safety pack for the paid buy path (no page or UI changes):

- One Stripe Checkout Session writes one order row. Agent polls of `/api/orders/status` and Stripe webhook retries return the same MCP URL instead of stacking duplicate orders.
- Stripe webhook event ids are remembered on the data volume; retries are acked with `"duplicate": true`.
- Wrong admin secrets (`/admin/login`, `/admin?secret=`, `X-Admin-Secret` / `admin_secret`) are counted per client; after `ADMIN_LOCK_MAX` (8) misses in `ADMIN_LOCK_WINDOW_SEC` (900) that client gets `429` until the window passes (global cap `ADMIN_LOCK_GLOBAL_MAX`, 60). Admin compares are constant-time.
- Public Checkout Session creation is limited to `ORDER_CHECKOUT_PER_HOUR` (20) per client; status polling to `ORDER_STATUS_PER_MIN` (120).
- `/health` shows `packs.order_guard` and counters. Kill switch: `ORDER_GUARD=0`. Ledger: `ORDER_GUARD_LEDGER` (default `$DATA_DIR/order_guard_ledger.json`).

Patterns (no code copied): Stripe "handle duplicate events" docs, hookdeck/webhook-skills (MIT), jazzband/django-axes (MIT), AdamPflug/express-brute (MIT), laurentS/slowapi (MIT).
