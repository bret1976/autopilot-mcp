# 6Frame Autopilot MCP

A sellable MCP: buyers paste one private URL into Claude, Claude Code, Cursor, ChatGPT, or Codex. They enter **their** Gemini and PostProxy keys. Autopilot runs the locked spine:

scan a viral AI-filmmaking original → download it → trim under 60s → write 6Frame-voice copy with hashtags → PostProxy publishes.

9:16 for Instagram, TikTok, YouTube Shorts, Facebook.  
16:9 for LinkedIn and X.  
YouTube titles include `#Shorts`.

Buyers work inside the LLM they already pay for. They paste one URL. They do not learn a 6Frame app.

**Price: `$997` once** — set in `app/config.py` as `PRICE_USD`. Landing, `/buy`, and `/admin` all read that constant.

Owner: Bret Jenny / 6Frame Studio.

## What a buyer does

1. Pay. Bret mints a signed URL (`/admin` or the command below).
2. Paste the URL into the model they already pay for.
3. Call `setup` with their `GEMINI_API_KEY` and `POSTPROXY_API_KEY`.
4. Connect socials on **their** PostProxy account (`postproxy_connect`).
5. `run_autopilot`.

### Claude.ai

Settings → Connectors → Add custom connector → paste:

`https://YOUR-HOST/mcp?token=SIGNED_TOKEN`

### Claude Code / Cursor / Codex `mcp.json`

```json
{
  "mcpServers": {
    "6frame-autopilot": {
      "url": "https://YOUR-HOST/mcp?token=SIGNED_TOKEN"
    }
  }
}
```

Some clients take the same URL plus `Authorization: Bearer SIGNED_TOKEN`.

## Tools

| Tool | Role |
| --- | --- |
| `setup` / `configure` | Save keys and brand. Secrets are never echoed. |
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

Or open `/admin`, enter `ADMIN_SECRET`, set buyer + days valid, Generate link. After a `/buy` order, send the URL by hand. Unauthenticated `/mcp` returns `401 Missing license key`.

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

Hero video is a **real screen walkthrough** of live Autopilot (`marketing-automation-production-dbd5.up.railway.app`). Drop the finished cut on:

- `public/promo.mp4`
- `public/poster.jpg` (first frame or a still from the same recording)

Do not replace those with title cards, talking-head, or stock B-roll. The YouTube theory clip is a footnote on the landing, not the hero.
