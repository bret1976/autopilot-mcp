from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass

from starlette.requests import Request

from app.config import issuer_secret

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{2,80}\.[0-9]{8,16}\.[a-f0-9]{32}$")


@dataclass(frozen=True)
class BuyerToken:
    buyer_id: str
    issued_at: int
    raw: str


def clean_buyer_id(buyer_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", buyer_id.strip().lower()).strip("-")
    if len(cleaned) < 2:
        raise ValueError("buyer_id must contain at least two letters or numbers")
    return cleaned[:80]


def mint_token(buyer_id: str, issued_at: int | None = None) -> str:
    buyer_id = clean_buyer_id(buyer_id)
    ts = int(issued_at or time.time())
    msg = f"{buyer_id}.{ts}"
    sig = hmac.new(issuer_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{buyer_id}.{ts}.{sig}"


def verify_token(token: str | None) -> BuyerToken | None:
    if not token:
        return None
    token = token.strip()
    if not _TOKEN_RE.match(token):
        return None
    buyer_id, ts_raw, sig = token.split(".")
    msg = f"{buyer_id}.{ts_raw}"
    expected = hmac.new(issuer_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return None
    return BuyerToken(buyer_id=buyer_id, issued_at=int(ts_raw), raw=token)


def extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth:
        parts = auth.split(None, 1)
        if len(parts) == 2 and parts[0].lower() in {"bearer", "token"}:
            return parts[1].strip()
        if len(parts) == 1:
            return parts[0].strip()
    header_token = request.headers.get("x-mcp-token") or request.headers.get("x-buyer-token")
    if header_token:
        return header_token.strip()
    return (
        request.query_params.get("token")
        or request.query_params.get("access_token")
        or request.path_params.get("token")
    )

