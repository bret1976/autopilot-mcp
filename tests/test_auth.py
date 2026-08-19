from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("ADMIN_SECRET", "test-admin")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-tests")

import pytest
from fastapi.testclient import TestClient

from app.config import PRICE_USD
from app.main import app
from app.mcp_server import mcp
from app.tokens import mint_token, verify_token

BANNED = (
    "Becker",
    "Hyros",
    "all-in-one platform",
    "start a free trial",
    "free trial",
    "Cormorant",
    "$997",
)


def test_price_is_297() -> None:
    assert PRICE_USD == 297


def test_health_and_landing() -> None:
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["price"] == 297
        page = client.get("/")
        assert page.status_code == 200
        text = page.text
        assert "$297" in text
        assert "Get the MCP link" in text
        assert "What you are actually buying" in text
        assert "Three steps. No install. No new app." in text
        assert "Run Autopilot for my studio." in text
        assert "9:16" in text
        assert "People are not going to work via softwares anymore" in text
        for phrase in BANNED:
            assert phrase not in text


def test_mcp_requires_license() -> None:
    with TestClient(app) as client:
        bare = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert bare.status_code == 401
        assert bare.json()["error"] == "license required"


def test_mcp_accepts_signed_token() -> None:
    token = mint_token("studio-alpha")
    assert verify_token(token) is not None
    with TestClient(app) as client:
        res = client.post(
            f"/mcp?token={token}",
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


def test_lead_endpoint() -> None:
    with TestClient(app) as client:
        res = client.post("/api/leads", json={"email": "buyer@studio.test", "studio": "North Light"})
        assert res.status_code == 200
        assert "Bret will send your private MCP URL" in res.json()["message"]
        assert res.json()["price"] == 297
