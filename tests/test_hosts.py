from __future__ import annotations

import json
import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-host-tests")

from fastapi.testclient import TestClient

from app.main import app
from app.tokens import mint_token

INIT_VERS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")


def _json(res):
    ctype = res.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in res.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise AssertionError(res.text[:300])
    return res.json()


def test_host_handshakes_and_fast_tools() -> None:
    token = mint_token("host-audit")
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["data_dir_writable"] is True
        assert client.get("/mcp").status_code == 401
        probe = client.get(f"/mcp/t/{token}", headers={"Accept": "*/*", "Origin": "https://grok.com"})
        assert probe.status_code == 200
        assert probe.json()["server"]["name"] == "TrendPilot"
        assert client.head(f"/mcp/t/{token}").status_code == 200
        opt = client.options(
            f"/mcp/t/{token}",
            headers={
                "Origin": "https://grok.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,accept",
            },
        )
        assert opt.status_code == 200
        assert opt.headers.get("access-control-allow-origin") == "*"

        for proto in INIT_VERS:
            res = client.post(
                f"/mcp/t/{token}",
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "MCP-Protocol-Version": proto,
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": proto,
                        "capabilities": {},
                        "clientInfo": {"name": "host-audit", "version": "0"},
                    },
                },
            )
            assert res.status_code == 200, proto
            assert _json(res)["result"]["serverInfo"]["name"] == "TrendPilot"

        for accept in ("application/json", "*/*", "application/json, text/event-stream", "text/event-stream"):
            res = client.post(
                f"/mcp/t/{token}",
                headers={"Accept": accept, "Content-Type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "a", "version": "0"},
                    },
                },
            )
            assert res.status_code == 200, accept

        note = client.post(
            f"/mcp/t/{token}",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert note.status_code in {200, 202}

        listed = client.post(
            f"/mcp/t/{token}",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        names = {t["name"] for t in _json(listed)["result"]["tools"]}
        assert {"onboard", "status", "run_autopilot", "scan_trends"}.issubset(names)

        onboard = client.post(
            f"/mcp/t/{token}",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "onboard", "arguments": {}},
            },
        )
        payload = _json(onboard)["result"]["structuredContent"]
        assert payload["needs_setup"] is True
        assert "gemini_api_key" in payload["missing"]

        status = client.post(
            f"/mcp/t/{token}",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "status", "arguments": {}}},
        )
        assert status.status_code == 200
        body = _json(status)["result"]["structuredContent"]
        assert body["busy"] is False
