from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass

from starlette.requests import Request

from app.config import issuer_secret, revoked_keys

_TOKEN_RE = re.compile(
    r"^[A-Za-z0-9_-]{2,80}\.[0-9]{8,16}(?:\.[0-9]{1,16})?\.[a-f0-9]{32}$"
)


@dataclass(frozen=True)
class BuyerToken:
    buyer_id: str
    issued_at: int
    expires_at: int
    raw: str


def clean_buyer_id(buyer_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", buyer_id.strip().lower()).strip("-")
    if len(cleaned) < 2:
        raise ValueError("buyer_id must contain at least two letters or numbers")
    return cleaned[:80]


def mint_token(buyer_id: str, issued_at: int | None = None, days: int = 0) -> str:
    buyer_id = clean_buyer_id(buyer_id)
    ts = int(issued_at or time.time())
    exp = 0 if int(days or 0) <= 0 else ts + int(days) * 86400
    msg = f"{buyer_id}.{ts}.{exp}"
    sig = hmac.new(issuer_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{buyer_id}.{ts}.{exp}.{sig}"


def verify_token(token: str | None) -> BuyerToken | None:
    if not token:
        return None
    token = token.strip()
    if token in revoked_keys():
        return None
    if not _TOKEN_RE.match(token):
        return None
    parts = token.split(".")
    if len(parts) == 3:
        buyer_id, ts_raw, sig = parts
        exp = 0
        msg = f"{buyer_id}.{ts_raw}"
    elif len(parts) == 4:
        buyer_id, ts_raw, exp_raw, sig = parts
        exp = int(exp_raw)
        msg = f"{buyer_id}.{ts_raw}.{exp}"
    else:
        return None
    if buyer_id in revoked_keys():
        return None
    expected = hmac.new(issuer_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    if exp and exp < int(time.time()):
        return None
    return BuyerToken(buyer_id=buyer_id, issued_at=int(ts_raw), expires_at=exp, raw=token)


def token_from_paste(raw: str | None) -> str | None:
    """Pull a license token out of a pasted MCP URL, Bearer header, or raw token."""
    if not raw:
        return None
    text = raw.strip().strip('"').strip("'")
    if not text:
        return None
    lower = text.lower()
    if lower.startswith("bearer ") or lower.startswith("token "):
        text = text.split(None, 1)[1].strip()
    if "token=" in text:
        text = text.split("token=", 1)[1]
    elif "/mcp/t/" in text:
        text = text.split("/mcp/t/", 1)[1]
    text = text.split("&", 1)[0].split("/", 1)[0].split("#", 1)[0].split("?", 1)[0]
    return text.strip() or None


def extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"bearer", "token"}:
            return parts[1].strip()
        if len(parts) == 1:
            return parts[0].strip()
    header_token = (
        request.headers.get("x-mcp-token")
        or request.headers.get("x-buyer-token")
        or request.headers.get("x-api-key")
    )
    if header_token:
        return header_token.strip()
    return token_from_paste(
        request.query_params.get("token")
        or request.query_params.get("access_token")
        or request.query_params.get("key")
        or request.path_params.get("token")
    )
