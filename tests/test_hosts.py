from __future__ import annotations

import asyncio
import json
import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-host-tests")

import pytest
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
        content = _json(status)["result"].get("content") or []
        assert content and content[0].get("text")


def test_initialized_with_id_is_not_32602() -> None:
    token = mint_token("init-id")
    with TestClient(app) as client:
        res = client.post(
            f"/mcp/t/{token}",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 99, "method": "notifications/initialized"},
        )
        assert res.status_code == 200
        payload = _json(res)
        assert payload.get("id") == 99
        assert "error" not in payload
        assert payload.get("result") == {}


def test_missing_content_type_still_initializes() -> None:
    token = mint_token("no-ctype")
    with TestClient(app) as client:
        res = client.post(
            f"/mcp/t/{token}",
            headers={"Accept": "application/json"},
            content=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "plain", "version": "0"},
                    },
                }
            ),
        )
        assert res.status_code == 200
        assert _json(res)["result"]["serverInfo"]["name"] == "TrendPilot"


def test_get_with_stale_session_is_not_405() -> None:
    token = mint_token("stale-session")
    with TestClient(app) as client:
        res = client.get(
            f"/mcp/t/{token}",
            headers={"Accept": "*/*", "Mcp-Session-Id": "deadbeef"},
        )
        assert res.status_code == 200
        assert res.json()["mcp"] is True


def test_bearer_and_query_and_path_are_same_license() -> None:
    token = mint_token("three-ways")
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "three", "version": "0"},
        },
    }
    with TestClient(app) as client:
        path = client.post(
            f"/mcp/t/{token}",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json=init,
        )
        query = client.post(
            f"/mcp?token={token}",
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json=init,
        )
        bearer = client.post(
            "/mcp",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json=init,
        )
        assert {_json(path)["result"]["serverInfo"]["name"], _json(query)["result"]["serverInfo"]["name"], _json(bearer)["result"]["serverInfo"]["name"]} == {"TrendPilot"}


def test_connector_probe_get_finishes_with_json() -> None:
    """Grok/ChatGPT GET both Accept types and wait for EOF. Must not stream."""
    token = mint_token("probe-host")
    path = f"/mcp/t/{token}"
    with TestClient(app) as client:
        for accept in (
            "*/*",
            "application/json",
            "application/json, text/event-stream",
        ):
            probe = client.get(path, headers={"Accept": accept, "Origin": "https://grok.com"})
            assert probe.status_code == 200, accept
            assert "application/json" in probe.headers.get("content-type", "")
            assert probe.json()["mcp"] is True
            assert probe.json()["server"]["name"] == "TrendPilot"


@pytest.mark.asyncio
async def test_eventsource_get_is_sse() -> None:
    token = mint_token("sse-host")
    path = f"/mcp/t/{token}"
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"accept", b"text/event-stream"), (b"host", b"test")],
        "client": ("testclient", 50000),
        "server": ("test", 80),
    }
    messages: list[dict] = []

    async def receive():
        await asyncio.sleep(0.05)
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    await asyncio.wait_for(app(scope, receive, send), timeout=2)
    start = next(item for item in messages if item["type"] == "http.response.start")
    headers = {key.decode(): value.decode() for key, value in start["headers"]}
    assert start["status"] == 200
    assert "text/event-stream" in headers["content-type"]
    body = b"".join(item.get("body") or b"" for item in messages if item["type"] == "http.response.body")
    assert body.startswith(b":")
