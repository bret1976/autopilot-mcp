"""Agent-buyer discovery surfaces (llms.txt, MCP Server Card, agent terms).

Backend-only. Does not change homepage or buy UI.
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
)

router = APIRouter(tags=["agent-discovery"])

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


def _base() -> str:
    return public_base_url().rstrip("/")


def llms_txt() -> str:
    base = _base()
    tool_lines = "\n".join(f"- `{name}` — {desc}" for name, desc in MCP_TOOLS)
    return f"""# {PRODUCT_NAME} ({STUDIO_NAME})

> Private MCP workflow for AI agents and LLM hosts. Buyers paste one licensed MCP URL into Claude, Grok, Codex, Cursor, Antigravity, or ChatGPT. Price: {PRICE_LABEL}.

This file is for AI agents and automated purchasers. Human buy page: {base}/buy

## Product

- Name: {PRODUCT_NAME} / MCP server name `{MCP_SERVER_NAME}`
- Owner: {OWNER_NAME} / {STUDIO_NAME}
- Studio site: {STUDIO_WEBSITE}
- Live: {base}
- Price: ${PRICE_USD} once (license unlocks MCP tools for 365 days on /buy mint)

## How an agent buys

1. Discover this file (`/llms.txt`) and the OpenAPI + MCP Server Card.
2. Purchase or mint a license: human `{base}/buy` or `POST {base}/api/orders` with JSON `{{"name","email"}}` (returns `mcp_url`).
3. Connect MCP at the returned path URL: `{base}/mcp/t/{{LICENSE_TOKEN}}` (also `Authorization: Bearer {{LICENSE_TOKEN}}`).
4. Call tools starting with `onboard` → `setup` → social connect → `run_autopilot(draft=true)`.

## Machine contracts

- OpenAPI JSON: {base}/openapi.json
- OpenAPI YAML: {base}/openapi.yaml
- Interactive docs: {base}/docs
- MCP Server Card: {base}/.well-known/mcp/server-card.json
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

- ${PRICE_USD} once per license (see config PRICE_USD)
- Metered overage is not enabled; one license unlocks the tool surface
- Do not invent ARR/MRR; treat sales figures as unknown unless Bret publishes them

## Contact

- Studio: {STUDIO_WEBSITE}
- Product owner: {OWNER_NAME}
"""


def agent_terms_md() -> str:
    base = _base()
    return f"""# {PRODUCT_NAME} — Agent / Automated Purchaser Terms (stub)

**Status:** Draft stub for agent discovery. Bret Jenny / counsel own the final legal text.

## Automated purchasing

- Automated agents may discover this product via `/llms.txt`, OpenAPI, and the MCP Server Card.
- License minting via `POST /api/orders` issues a private MCP URL. Treat that URL as a secret credential.
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
    return {
        "name": MCP_SERVER_NAME,
        "description": (
            f"{PRODUCT_NAME} — licensed MCP for AI filmmaking autopilot. "
            f"Scan a viral original, trim, write copy, publish via PostProxy. "
            f"{PRICE_LABEL}."
        ),
        "version": "0.1.0",
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
            "price_usd": PRICE_USD,
            "price_label": PRICE_LABEL,
            "buy_url": f"{base}/buy",
            "orders_api": f"{base}/api/orders",
            "terms": f"{base}/agent-terms.md",
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
        "tools": [{"name": name, "description": desc} for name, desc in MCP_TOOLS],
        "health": f"{base}/health",
        "discovery": {
            "llms_txt": f"{base}/llms.txt",
            "server_card": f"{base}/.well-known/mcp/server-card.json",
            "openapi_json": f"{base}/openapi.json",
            "openapi_yaml": f"{base}/openapi.yaml",
        },
    }


@router.get("/llms.txt", response_class=PlainTextResponse)
async def get_llms_txt() -> PlainTextResponse:
    return PlainTextResponse(llms_txt(), media_type="text/plain; charset=utf-8")


@router.get("/agent-terms.md", response_class=PlainTextResponse)
async def get_agent_terms() -> PlainTextResponse:
    return PlainTextResponse(agent_terms_md(), media_type="text/markdown; charset=utf-8")


@router.get("/.well-known/mcp/server-card.json")
async def get_server_card() -> dict[str, Any]:
    return server_card()


@router.get("/openapi.yaml")
async def get_openapi_yaml():
    """Point agents at the canonical OpenAPI JSON (no extra YAML dependency)."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/openapi.json", status_code=307)
