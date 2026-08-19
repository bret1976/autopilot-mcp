from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("ADMIN_SECRET", "test-admin")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-tests")
os.environ["REVOKED_KEYS"] = "revoked-studio"

import pytest
from fastapi.testclient import TestClient

from app.config import PRICE_USD
from app.main import app
from app.mcp_server import mcp
from app.tokens import mint_token, verify_token

BANNED = (
    "Becker",
    "Hyros",
    "Realtor OS",
    "HighLevel",
    "GoHighLevel",
    "Zillow",
    "Austin",
    "all-in-one platform",
    "start a free trial",
    "$297",
)


def test_price_is_997() -> None:
    assert PRICE_USD == 997


def test_health_and_landing() -> None:
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["price"] == 997
        page = client.get("/")
        assert page.status_code == 200
        text = page.text
        assert "Cormorant+Garamond" in text
        assert "Manrope" in text
        css = client.get("/assets/page.css").text
        assert "--bg:#0c0f0d" in css
        assert "--gold:#c4a574" in css
        assert "border-radius:999px" in css
        assert "$997" in text
        assert "Get the link · $997 once" in text
        assert "Get access · $997" in text
        assert "What you are actually buying" in text
        assert "Three steps. No install. No new app." in text
        assert "Run Autopilot for my studio." in text
        assert "Codex" in text
        assert "9:16" in text
        assert "People are not going to work via softwares anymore" in text
        assert "not a dashboard" in text.lower()
        assert 'src="/promo.mp4"' in text
        assert 'poster="/poster.jpg"' in text
        for phrase in BANNED:
            assert phrase not in text


def test_promo_slot_exists() -> None:
    from pathlib import Path

    from app.config import PUBLIC_DIR

    assert (PUBLIC_DIR / "promo.mp4").exists()
    assert (PUBLIC_DIR / "poster.jpg").exists()


def test_buy_form() -> None:
    with TestClient(app) as client:
        page = client.get("/buy")
        assert page.status_code == 200
        assert "Checkout" in page.text
        assert 'name="name"' in page.text
        assert 'name="email"' in page.text
        assert 'name="client"' in page.text
        assert "Codex" in page.text
        assert "No card is charged on this page" in page.text


def test_mcp_requires_license() -> None:
    with TestClient(app) as client:
        bare = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert bare.status_code == 401
        assert bare.json()["error"] == "Missing license key"


def test_mcp_accepts_signed_token() -> None:
    token = mint_token("studio-alpha")
    assert verify_token(token) is not None
    with TestClient(app) as client:
        res = client.post(
            f"/mcp?token={token}",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "tests", "version": "0"},
                },
            },
        )
        assert res.status_code != 401


def test_revoked_and_expired_tokens() -> None:
    assert verify_token(mint_token("revoked-studio")) is None
    dead = mint_token("old-studio", issued_at=1_700_000_000, days=1)
    assert verify_token(dead) is None
    live = mint_token("live-studio", days=30)
    assert verify_token(live) is not None


@pytest.mark.asyncio
async def test_tools_are_registered() -> None:
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {
        "setup",
        "postproxy_status",
        "postproxy_connect",
        "scan_trends",
        "download_original",
        "write_copy",
        "publish",
        "run_autopilot",
        "status",
    }.issubset(names)


def test_orders_endpoint() -> None:
    with TestClient(app) as client:
        res = client.post(
            "/api/orders",
            json={"name": "Nia", "email": "buyer@studio.test", "client": "Codex", "studio": "North Light"},
        )
        assert res.status_code == 200
        assert "You are in line" in res.json()["message"]
        assert res.json()["price"] == 997
