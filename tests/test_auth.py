from __future__ import annotations

import json
import os
import subprocess

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("ADMIN_SECRET", "test-admin")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-tests")
os.environ["REVOKED_KEYS"] = "revoked-studio"

import pytest
from fastapi.testclient import TestClient

from app.config import PRICE_USD, PUBLIC_DIR
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
    "realtor",
    "all-in-one platform",
    "start a free trial",
    "$297",
)

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "tests", "version": "0"},
    },
}

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


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
        css = client.get("/assets/page.css").text
        assert "Cormorant+Garamond" in text
        assert "Manrope" in text
        assert "--bg:#0c0f0d" in css
        assert "--gold:#c4a574" in css
        assert "border-radius:999px" in css
        assert "$997" in text
        assert "Get the link · $997 once" in text
        assert "Buy the Autopilot workflow." in text
        assert "not another dashboard" in text.lower()
        assert "What you are actually buying" in text
        assert "Three steps. No install. No new app." in text
        assert "What do I get after I fill the form?" in text
        assert "Run Autopilot for my studio." in text
        assert "Codex" in text
        assert "9:16" in text
        assert 'src="/promo.mp4"' in text
        assert "Live Autopilot" in text
        assert "scan" in text.lower()
        for phrase in BANNED:
            assert phrase not in text
            assert phrase not in css


def test_promo_is_hybrid_value_and_live_app() -> None:
    promo = PUBLIC_DIR / "promo.mp4"
    poster = PUBLIC_DIR / "poster.jpg"
    assert promo.exists() and promo.stat().st_size > 400_000
    assert poster.exists() and poster.stat().st_size > 10_000
    probe = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(promo),
        ],
        text=True,
    ).strip()
    duration = float(probe)
    assert 45 <= duration <= 75


def test_buy_form() -> None:
    with TestClient(app) as client:
        page = client.get("/buy")
        assert page.status_code == 200
        assert 'name="name"' in page.text
        assert 'name="email"' in page.text
        assert 'name="client"' in page.text
        assert "Codex" in page.text
        assert "No card is charged on this page" in page.text


def test_mcp_requires_license_no_http_redirect() -> None:
    with TestClient(app, follow_redirects=False) as client:
        for path in ("/mcp", "/mcp/"):
            bare = client.post(
                path,
                headers=MCP_HEADERS,
                json=INIT,
            )
            assert bare.status_code == 401, path
            assert bare.json()["error"] == "Missing license key"
            location = bare.headers.get("location") or ""
            assert "http://" not in location


def test_mcp_https_slash_keeps_token() -> None:
    token = mint_token("studio-slash")
    with TestClient(app, follow_redirects=False, base_url="https://testserver") as client:
        res = client.post(
            f"/mcp?token={token}",
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
            json=INIT,
        )
        location = res.headers.get("location") or ""
        assert not location.startswith("http://")
        if res.status_code in {301, 302, 307, 308}:
            assert location.startswith("https://")
            assert f"token={token}" in location
        else:
            assert res.status_code != 401


def test_mcp_initialize_and_tools() -> None:
    token = mint_token("studio-alpha")
    assert verify_token(token) is not None
    with TestClient(app) as client:
        res = client.post(
            f"/mcp?token={token}",
            headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
            json=INIT,
        )
        assert res.status_code == 200
        payload = _jsonrpc(res)
        assert payload.get("result", {}).get("capabilities") is not None
        session = res.headers.get("mcp-session-id")
        listed = client.post(
            f"/mcp?token={token}",
            headers={
                **MCP_HEADERS,
                "Authorization": f"Bearer {token}",
                **({"Mcp-Session-Id": session} if session else {}),
            },
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert listed.status_code == 200
        tools_payload = _jsonrpc(listed)
        names = {tool["name"] for tool in tools_payload["result"]["tools"]}
        assert {
            "setup",
            "scan_trends",
            "download_original",
            "write_copy",
            "publish",
            "run_autopilot",
            "postproxy_connect",
            "status",
        }.issubset(names)


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
        "scan_trends",
        "download_original",
        "write_copy",
        "publish",
        "run_autopilot",
        "postproxy_connect",
        "status",
    }.issubset(names)


def test_orders_mint_url() -> None:
    with TestClient(app) as client:
        res = client.post(
            "/api/orders",
            json={"name": "Nia", "email": "buyer@studio.test", "client": "Codex"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["price"] == 997
        assert body["days"] == 365
        assert "/mcp?token=" in body["url"]
        assert body["url"] == body["mcp_url"]
        assert "You are in line" not in body["message"]
        token = body["url"].split("token=", 1)[1]
        assert verify_token(token) is not None

        html = client.post(
            "/api/orders",
            data={"name": "Nia", "email": "buyer@studio.test", "client": "Claude.ai"},
            headers={"Accept": "text/html"},
        )
        assert html.status_code == 200
        assert "/mcp?token=" in html.text
        assert "Copy the URL" in html.text
        assert "Add custom connector" in html.text
        assert "mcpServers" in html.text
        assert "You are in line" not in html.text


def _jsonrpc(response) -> dict:
    ctype = response.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise AssertionError(f"no JSON-RPC event in SSE: {response.text[:400]}")
    return response.json()
