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


def test_price_is_997() -> None:
    assert PRICE_USD == 997


def test_health_and_landing() -> None:
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["price"] == 997
        page = client.get("/")
        assert page.status_code == 200
        assert "$997" in page.text
        assert "$297" not in page.text
        assert "Cormorant" not in page.text
        assert "Get access · $297" not in page.text
        assert "SEQ 01" in page.text
        assert "9:16" in page.text


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
        assert res.json()["price"] == 997
