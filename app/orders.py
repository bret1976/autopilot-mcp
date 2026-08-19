from __future__ import annotations

from typing import Any

from fastapi import Request

from app.config import PRICE_USD, public_base_url
from app.store import append_lead, ensure_buyer, load_buyer, save_buyer
from app.tokens import clean_buyer_id, mint_token

LICENSE_DAYS = 365


def buyer_id_from_email(email: str) -> str:
    local, _, domain = email.strip().lower().partition("@")
    return clean_buyer_id(f"{local}-at-{domain}")


def _public_base(request: Request | None = None) -> str:
    base = public_base_url().rstrip("/")
    if request is not None:
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https")
        proto = proto.split(",")[0].strip()
        if proto == "https" and base.startswith("http://"):
            base = "https://" + base[len("http://") :]
    return base


def mcp_public_url(token: str, request: Request | None = None) -> str:
    """Path token survives hosts (Grok) that strip ?query secrets."""
    return f"{_public_base(request)}/mcp/t/{token}"


def mcp_query_url(token: str, request: Request | None = None) -> str:
    return f"{_public_base(request)}/mcp?token={token}"


def fulfill_order(
    *,
    name: str,
    email: str,
    client: str = "",
    studio: str = "",
    source: str = "buy",
    request: Request | None = None,
) -> dict[str, Any]:
    buyer_id = buyer_id_from_email(email)
    existing = load_buyer(buyer_id)
    if existing:
        token = str(existing.get("token") or mint_token(buyer_id, days=LICENSE_DAYS))
        dirty = False
        if not existing.get("token"):
            existing["token"] = token
            dirty = True
        if email and not existing.get("email"):
            existing["email"] = email
            dirty = True
        record = save_buyer(existing) if dirty else existing
    else:
        token = mint_token(buyer_id, days=LICENSE_DAYS)
        record = ensure_buyer(
            buyer_id,
            email=email,
            note=studio or name,
            token=token,
        )
    url = mcp_public_url(token, request)
    order = append_lead(
        {
            "name": name,
            "email": email,
            "client": client,
            "studio": studio,
            "source": source,
            "buyer_id": buyer_id,
            "mcp_url": url,
        }
    )
    return {
        "ok": True,
        "url": url,
        "mcp_url": url,
        "buyer_id": buyer_id,
        "order_id": order["id"],
        "order": order,
        "buyer": record,
        "token": token,
        "days": LICENSE_DAYS,
        "price": PRICE_USD,
        "message": "Your private MCP URL is ready.",
    }
