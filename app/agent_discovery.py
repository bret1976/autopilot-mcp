"""Agent-buyer discovery surfaces (llms.txt, MCP Server Card, agent terms, pricing).

Backend-only. Does not change homepage or buy UI design/layout.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.config import (
    MCP_SERVER_NAME,
    OWNER_NAME,
    PRICE_LABEL,
    PRICE_USD,
    PRODUCT_NAME,
    STUDIO_NAME,
    STUDIO_WEBSITE,
    public_base_url,
    stripe_payment_link,
)

router = APIRouter(tags=["agent-discovery"])

SKU_ID = "autopilot-mcp-license"
SKU_CURRENCY = "USD"
LICENSE_DAYS = 365

MCP_TOOLS = (
    ("onboard", "First call. Lists missing keys and brand questions."),
    ("start", "Alias of onboard."),
    ("setup", "Save buyer Gemini/PostProxy keys and brand. Secrets never echoed."),
    ("configure", "Alias of setup."),
    ("set_brand_from_website", "Pull brand voice from a website homepage."),
    ("postproxy_status", "Buyer PostProxy social connection status."),
    ("postproxy_connect", "Return OAuth links for buyer social accounts."),
    ("scan_trends", "Gemini-grounded scan for a viral original."),
    ("download_original", "Download and trim master clips (9:16 and 16:9)."),
    ("write_copy", "Write platform copy with locked hashtags."),
    ("publish", "Publish via PostProxy split by aspect."),
    ("run_autopilot", "Run the locked spine end-to-end (draft=true first)."),
    ("set_automation", "Enable or update the buyer daily automation job."),
    ("confirm_schedule", "Confirm pending automation schedule."),
    ("choose_start", "Choose how to start after schedule confirm."),
    ("approve_and_publish", "Approve a staged draft and publish."),
    ("status", "Buyer status / readiness."),
    ("proof_link", "Link to last proof page."),
    ("last_run", "Summary of the last autopilot run."),
)

CAPABILITIES = (
    "licensed_mcp_http",
    "viral_scan",
    "clip_trim",
    "platform_copy",
    "postproxy_publish",
    "daily_automation",
    "buyer_owned_keys",
)


def _base() -> str:
    return public_base_url().rstrip("/")


def pricing_block() -> dict[str, Any]:
    base = _base()
    payment_link = stripe_payment_link() or None
    return {
        "sku": SKU_ID,
        "product": PRODUCT_NAME,
        "mcp_server_name": MCP_SERVER_NAME,
        "price_usd": PRICE_USD,
        "price_label": PRICE_LABEL,
        "currency": SKU_CURRENCY,
        "billing": "one_time",
        "license_days": LICENSE_DAYS,
        "quantity": 1,
        "includes": [
            "Private MCP URL path token",
            "Access to all TrendPilot tools for the license term",
            "Buyer supplies own Gemini + PostProxy credentials",
        ],
        "does_not_include": [
            "Gemini API spend",
            "PostProxy spend",
            "Hosted social OAuth beyond PostProxy",
        ],
        "buy_url": f"{base}/buy",
        "orders_api": f"{base}/api/orders",
        "orders_method": "POST",
        "orders_body": {"name": "string", "email": "string", "source": "agent"},
        "stripe_payment_link": payment_link,
        "payment_collection": (
            "stripe_payment_link"
            if payment_link
            else "license_mint_then_invoice_or_manual"
        ),
        "agent_note": (
            "List price is $997 once. Do not invent discounts. "
            "POST /api/orders mints a private mcp_url. Collect payment per studio policy "
            "before treating a mint as a paid sale when no Stripe keys are configured."
        ),
        "terms": f"{base}/agent-terms.md",
    }


def llms_txt() -> str:
    base = _base()
    tool_lines = "\n".join(f"- `{name}` — {desc}" for name, desc in MCP_TOOLS)
    pricing = pricing_block()
    return f"""# {PRODUCT_NAME} ({STUDIO_NAME})

> Private MCP workflow for AI agents and LLM hosts. Buyers paste one licensed MCP URL into Claude, Grok, Codex, Cursor, Antigravity, or ChatGPT. Price: {PRICE_LABEL}.

This file is for AI agents and automated purchasers. Human buy page: {base}/buy

## Product

- Name: {PRODUCT_NAME} / MCP server name `{MCP_SERVER_NAME}`
- Owner: {OWNER_NAME} / {STUDIO_NAME}
- Studio site: {STUDIO_WEBSITE}
- Live: {base}
- Price: ${PRICE_USD} once (SKU `{SKU_ID}`, {LICENSE_DAYS}-day license on mint)
- Contact: {OWNER_NAME} via {STUDIO_WEBSITE}

## How an agent buys

1. Discover this file (`/llms.txt`), `/api/pricing`, OpenAPI, and the MCP Server Card.
2. Purchase or mint a license:
   - Human: `{base}/buy`
   - Agent JSON: `POST {base}/api/orders` with `{{"name","email","source":"agent"}}` and `Accept: application/json`
   - Response includes `mcp_url`, `sku`, `price_usd`, `instructions`, and host connect snippets.
3. Connect MCP at the returned path URL: `{base}/mcp/t/{{LICENSE_TOKEN}}` (also `Authorization: Bearer {{LICENSE_TOKEN}}`).
4. Call tools starting with `onboard` → `setup` → social connect → `run_autopilot(draft=true)`.

## Machine contracts

- Pricing / SKU JSON: {base}/api/pricing
- OpenAPI JSON: {base}/openapi.json
- OpenAPI YAML: {base}/openapi.yaml
- Interactive docs: {base}/docs
- MCP Server Card: {base}/.well-known/mcp/server-card.json
- Agent discovery pointer: {base}/.well-known/agent.json
- robots.txt: {base}/robots.txt
- Health: {base}/health
- Agent terms: {base}/agent-terms.md
- OAuth discovery (license hosts): {base}/.well-known/oauth-authorization-server

## MCP tools

{tool_lines}

## Auth

- Unauthenticated `/mcp` → 401 Missing license key
- Licensed URL form: `{base}/mcp/t/SIGNED_TOKEN`
- Query form still works: `{base}/mcp?token=SIGNED_TOKEN`
- Buyer Gemini and PostProxy keys are stored per license; never returned by tools

## Pricing (public)

- SKU: `{pricing["sku"]}`
- ${PRICE_USD} once per license (`{pricing["currency"]}`, one_time)
- Metered overage is not enabled; one license unlocks the tool surface
- Do not invent ARR/MRR or discounts; treat sales figures as unknown unless Bret publishes them
- Payment collection mode: `{pricing["payment_collection"]}`

## Capabilities

{chr(10).join(f"- `{c}`" for c in CAPABILITIES)}

## Contact

- Studio: {STUDIO_WEBSITE}
- Product owner: {OWNER_NAME}
"""


def agent_terms_md() -> str:
    base = _base()
    return f"""# {PRODUCT_NAME} — Agent / Automated Purchaser Terms (stub)

**Status:** Draft stub for agent discovery. Bret Jenny / counsel own the final legal text.

## Automated purchasing

- Automated agents may discover this product via `/llms.txt`, `/api/pricing`, OpenAPI, and the MCP Server Card.
- License minting via `POST /api/orders` issues a private MCP URL. Treat that URL as a secret credential.
- List price is ${PRICE_USD} once (SKU `{SKU_ID}`). Do not assume a discount for agent buyers.
- Do not share one license across unrelated tenants without a platform agreement.

## Allowed use

- Run the Autopilot spine for the licensed buyer brand using that buyer’s own Gemini and PostProxy credentials.
- Prefer `run_autopilot(draft=true)` before live publish.
- Respect platform rules of Instagram, TikTok, YouTube, Facebook, LinkedIn, and X.

## Prohibited

- Reselling or publishing license URLs
- Scraping or exfiltrating other buyers’ data from shared infrastructure
- Using the product to post prohibited, deceptive, or copyright-violating content
- Circumventing rate limits or health checks

## Refunds

- Metered usage after a successful job is generally non-refundable.
- License refunds follow the studio’s human sales policy (not defined in this stub).

## Data

- Uploaded media and buyer keys are stored for Autopilot operation under `{base}`.
- Retention and deletion requests: contact {STUDIO_NAME} via {STUDIO_WEBSITE}.

## Contact for disputes

- {OWNER_NAME} / {STUDIO_NAME} — {STUDIO_WEBSITE}
- Live product: {base}
"""


def server_card() -> dict[str, Any]:
    base = _base()
    pricing = pricing_block()
    return {
        "name": MCP_SERVER_NAME,
        "description": (
            f"{PRODUCT_NAME} — licensed MCP for AI filmmaking autopilot. "
            f"Scan a viral original, trim, write copy, publish via PostProxy. "
            f"{PRICE_LABEL}."
        ),
        "version": "0.1.1",
        "vendor": {
            "name": STUDIO_NAME,
            "url": STUDIO_WEBSITE,
            "contact": OWNER_NAME,
        },
        "homepage": base,
        "documentation": f"{base}/llms.txt",
        "openapi": f"{base}/openapi.json",
        "license": {
            "type": "commercial",
            "sku": SKU_ID,
            "price_usd": PRICE_USD,
            "price_label": PRICE_LABEL,
            "currency": SKU_CURRENCY,
            "billing": "one_time",
            "license_days": LICENSE_DAYS,
            "buy_url": f"{base}/buy",
            "orders_api": f"{base}/api/orders",
            "pricing_api": f"{base}/api/pricing",
            "terms": f"{base}/agent-terms.md",
            "payment_collection": pricing["payment_collection"],
        },
        "transport": {
            "type": "http",
            "url_template": f"{base}/mcp/t/{{LICENSE_TOKEN}}",
            "url_query_template": f"{base}/mcp?token={{LICENSE_TOKEN}}",
            "authorization": "Bearer LICENSE_TOKEN or path token",
        },
        "authentication": {
            "required": True,
            "schemes": ["license_path_token", "bearer_token", "query_token"],
            "unauthenticated_mcp": {
                "status": 401,
                "error": "Missing license key",
            },
        },
        "capabilities": list(CAPABILITIES),
        "tools": [{"name": name, "description": desc} for name, desc in MCP_TOOLS],
        "health": f"{base}/health",
        "discovery": {
            "llms_txt": f"{base}/llms.txt",
            "server_card": f"{base}/.well-known/mcp/server-card.json",
            "agent_json": f"{base}/.well-known/agent.json",
            "pricing": f"{base}/api/pricing",
            "openapi_json": f"{base}/openapi.json",
            "openapi_yaml": f"{base}/openapi.yaml",
            "robots": f"{base}/robots.txt",
            "terms": f"{base}/agent-terms.md",
        },
        "contact": {
            "name": OWNER_NAME,
            "studio": STUDIO_NAME,
            "url": STUDIO_WEBSITE,
        },
    }


def agent_pointer() -> dict[str, Any]:
    base = _base()
    return {
        "name": PRODUCT_NAME,
        "description": f"{PRODUCT_NAME} licensed MCP ({PRICE_LABEL})",
        "type": "mcp_server",
        "url": base,
        "mcp": f"{base}/.well-known/mcp/server-card.json",
        "llms": f"{base}/llms.txt",
        "openapi": f"{base}/openapi.json",
        "pricing": f"{base}/api/pricing",
        "terms": f"{base}/agent-terms.md",
        "buy": f"{base}/buy",
        "orders": f"{base}/api/orders",
        "health": f"{base}/health",
        "price_usd": PRICE_USD,
        "sku": SKU_ID,
    }


def robots_txt() -> str:
    base = _base()
    return f"""User-agent: *
Allow: /
Allow: /llms.txt
Allow: /openapi.json
Allow: /openapi.yaml
Allow: /api/pricing
Allow: /agent-terms.md
Allow: /.well-known/
Allow: /health
Allow: /buy
Allow: /docs
Disallow: /admin
Disallow: /mcp
Disallow: /api/orders
Disallow: /media/

# Agent discovery
# llms.txt: {base}/llms.txt
# MCP Server Card: {base}/.well-known/mcp/server-card.json
# Pricing: {base}/api/pricing
# OpenAPI: {base}/openapi.json
"""


def order_agent_payload(fulfilled: dict[str, Any]) -> dict[str, Any]:
    """Enrich POST /api/orders JSON for agents without changing buy HTML."""
    base = _base()
    url = fulfilled["url"]
    token = fulfilled["token"]
    pricing = pricing_block()
    return {
        "ok": True,
        "message": fulfilled["message"],
        "sku": SKU_ID,
        "product": PRODUCT_NAME,
        "price": PRICE_USD,
        "price_usd": PRICE_USD,
        "price_label": PRICE_LABEL,
        "currency": SKU_CURRENCY,
        "billing": "one_time",
        "license_days": fulfilled.get("days", LICENSE_DAYS),
        "days": fulfilled.get("days", LICENSE_DAYS),
        "payment_status": "license_issued",
        "payment_collection": pricing["payment_collection"],
        "url": url,
        "mcp_url": url,
        "mcp_query_url": f"{base}/mcp?token={token}",
        "order_id": fulfilled["order_id"],
        "buyer_id": fulfilled["buyer_id"],
        "instructions": {
            "connect": (
                f"Paste mcp_url into Claude/Grok/ChatGPT custom connector, "
                f"or set Authorization: Bearer <token> for Cursor/Codex/Antigravity."
            ),
            "first_tools": ["onboard", "setup", "postproxy_connect", "run_autopilot"],
            "prefer_draft": True,
            "docs": f"{base}/llms.txt",
            "terms": f"{base}/agent-terms.md",
            "server_card": f"{base}/.well-known/mcp/server-card.json",
            "pricing": f"{base}/api/pricing",
        },
        "auth": {
            "type": "license_token",
            "path_url": url,
            "bearer_header": "Authorization: Bearer <LICENSE_TOKEN>",
            "unauthenticated_mcp_status": 401,
        },
        "host_snippets": {
            "claude_grok_chatgpt": {"url": url},
            "cursor_codex": {
                "mcpServers": {
                    "6frame-autopilot": {
                        "url": url,
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                }
            },
            "antigravity": {
                "mcpServers": {
                    "6frame-autopilot": {
                        "serverUrl": url,
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                }
            },
        },
    }


DISCOVERY_LINK_HEADER = (
    '</llms.txt>; rel="llms-txt", '
    '</.well-known/mcp/server-card.json>; rel="mcp-server-card", '
    '</api/pricing>; rel="pricing", '
    '</openapi.json>; rel="service-desc", '
    '</agent-terms.md>; rel="terms", '
    '</.well-known/agent.json>; rel="agent-discovery"'
)


class AgentDiscoveryHeadersMiddleware:
    """Attach agent discovery Link headers on public GET responses (no UI change)."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = scope.get("path") or ""
        skip = path.startswith("/mcp") or path.startswith("/admin") or path.startswith("/media")
        if method not in {"GET", "HEAD"} or skip:

            async def send_passthrough(message):
                await send(message)

            await self.app(scope, receive, send_passthrough)
            return

        async def send_with_link(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                headers.append((b"link", DISCOVERY_LINK_HEADER.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_link)


@router.get("/llms.txt", response_class=PlainTextResponse)
async def get_llms_txt() -> PlainTextResponse:
    return PlainTextResponse(llms_txt(), media_type="text/plain; charset=utf-8")


@router.get("/agent-terms.md", response_class=PlainTextResponse)
async def get_agent_terms() -> PlainTextResponse:
    return PlainTextResponse(agent_terms_md(), media_type="text/markdown; charset=utf-8")


@router.get("/.well-known/mcp/server-card.json")
async def get_server_card() -> dict[str, Any]:
    return server_card()


@router.get("/.well-known/agent.json")
async def get_agent_json() -> dict[str, Any]:
    return agent_pointer()


@router.get("/robots.txt", response_class=PlainTextResponse)
async def get_robots() -> PlainTextResponse:
    return PlainTextResponse(robots_txt(), media_type="text/plain; charset=utf-8")


@router.get("/api/pricing")
async def get_pricing() -> dict[str, Any]:
    return pricing_block()


@router.get("/openapi.yaml")
async def get_openapi_yaml():
    """Point agents at the canonical OpenAPI JSON (no extra YAML dependency)."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse("/openapi.json", status_code=307)
