from __future__ import annotations

import base64
import hashlib
import json
import secrets

from fastapi.testclient import TestClient

from app.main import app
from app.tokens import mint_token, token_from_paste, verify_token

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "grok", "version": "0"},
    },
}


def _jsonrpc(response) -> dict:
    ctype = response.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in response.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise AssertionError(f"no JSON-RPC event in SSE: {response.text[:400]}")
    return response.json()


def test_token_from_paste_reads_path_and_query() -> None:
    token = mint_token("paste-studio")
    assert token_from_paste(f"https://host/mcp/t/{token}") == token
    assert token_from_paste(f"https://host/mcp?token={token}") == token
    assert token_from_paste(f"Bearer {token}") == token
    assert verify_token(token_from_paste(token)) is not None


def test_path_token_initialize() -> None:
    token = mint_token("path-studio")
    with TestClient(app) as client:
        res = client.post(
            f"/mcp/t/{token}",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json=INIT,
        )
        assert res.status_code == 200
        assert _jsonrpc(res)["result"]["capabilities"] is not None


def test_json_only_accept_does_not_406() -> None:
    token = mint_token("json-studio")
    with TestClient(app) as client:
        res = client.post(
            f"/mcp/t/{token}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://grok.com",
            },
            json=INIT,
        )
        assert res.status_code == 200
        assert "application/json" in res.headers.get("content-type", "")
        payload = res.json()
        assert payload["result"]["serverInfo"]["name"] == "TrendPilot"


def test_star_accept_initialize() -> None:
    token = mint_token("star-studio")
    with TestClient(app) as client:
        res = client.post(
            f"/mcp?token={token}",
            headers={"Accept": "*/*", "Content-Type": "application/json"},
            json=INIT,
        )
        assert res.status_code == 200
        assert _jsonrpc(res)["result"]["capabilities"] is not None


def test_bearer_without_query() -> None:
    token = mint_token("bearer-studio")
    with TestClient(app) as client:
        res = client.post(
            "/mcp",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json=INIT,
        )
        assert res.status_code == 200


def test_well_known_oauth_discovery() -> None:
    with TestClient(app) as client:
        resource = client.get("/.well-known/oauth-protected-resource")
        assert resource.status_code == 200
        body = resource.json()
        assert body["resource"].endswith("/mcp")
        assert body["authorization_servers"]
        server = client.get("/.well-known/oauth-authorization-server")
        assert server.status_code == 200
        meta = server.json()
        assert meta["authorization_endpoint"].endswith("/oauth/authorize")
        assert meta["token_endpoint"].endswith("/oauth/token")
        under_mcp = client.get("/mcp/.well-known/oauth-protected-resource")
        assert under_mcp.status_code == 200
        assert under_mcp.json()["resource"].endswith("/mcp")
        token = mint_token("wk-path")
        under_token = client.get(f"/mcp/t/{token}/.well-known/oauth-protected-resource")
        assert under_token.status_code == 200
        assert under_token.json()["resource"].endswith(f"/mcp/t/{token}")
        rfc9728 = client.get(f"/.well-known/oauth-protected-resource/mcp/t/{token}")
        assert rfc9728.status_code == 200
        assert rfc9728.json()["resource"].endswith(f"/mcp/t/{token}")
        authz_suffix = client.get(f"/.well-known/oauth-authorization-server/mcp/t/{token}")
        assert authz_suffix.status_code == 200
        assert authz_suffix.json()["authorization_endpoint"].endswith("/oauth/authorize")
        appended = client.get(f"/mcp/t/{token}/mcp", headers={"Accept": "*/*"})
        assert appended.status_code == 200
        assert appended.json()["mcp"] is True


def test_oauth_license_exchange() -> None:
    token = mint_token("oauth-studio")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with TestClient(app, follow_redirects=False) as client:
        registered = client.post(
            "/oauth/register",
            json={
                "client_name": "Grok",
                "redirect_uris": ["https://grok.com/oauth/callback"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert registered.status_code == 201
        client_id = registered.json()["client_id"]
        form = client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://grok.com/oauth/callback",
                "response_type": "code",
                "state": "abc",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert form.status_code == 200
        assert "Connect your license" in form.text
        submitted = client.post(
            "/oauth/authorize",
            data={
                "license": f"https://host/mcp/t/{token}",
                "client_id": client_id,
                "redirect_uri": "https://grok.com/oauth/callback",
                "state": "abc",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            },
        )
        assert submitted.status_code == 302
        location = submitted.headers["location"]
        assert location.startswith("https://grok.com/oauth/callback?")
        assert "code=" in location
        code = location.split("code=", 1)[1].split("&", 1)[0]
        exchanged = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://grok.com/oauth/callback",
                "code_verifier": verifier,
                "client_id": client_id,
            },
        )
        assert exchanged.status_code == 200
        assert exchanged.json()["access_token"] == token
        live = client.post(
            "/mcp",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {exchanged.json()['access_token']}",
                "Origin": "https://grok.com",
            },
            json=INIT,
        )
        assert live.status_code == 200
        assert live.json()["result"]["capabilities"] is not None


def test_oauth_silent_when_resource_already_has_token() -> None:
    token = mint_token("silent-studio")
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with TestClient(app, follow_redirects=False) as client:
        registered = client.post(
            "/oauth/register",
            json={"client_name": "Grok", "redirect_uris": ["https://grok.com/oauth/callback"]},
        )
        client_id = registered.json()["client_id"]
        silent = client.get(
            "/oauth/authorize",
            params={
                "client_id": client_id,
                "redirect_uri": "https://grok.com/oauth/callback",
                "response_type": "code",
                "state": "xyz",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": f"https://autopilot.example/mcp/t/{token}",
            },
        )
        assert silent.status_code == 302, silent.text[:300]
        location = silent.headers["location"]
        assert "code=" in location
        code = location.split("code=", 1)[1].split("&", 1)[0]
        exchanged = client.post(
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": "https://grok.com/oauth/callback",
                "code_verifier": verifier,
                "client_id": client_id,
            },
        )
        assert exchanged.status_code == 200
        assert exchanged.json()["access_token"] == token


def test_both_accept_initialize_is_json_not_sse() -> None:
    token = mint_token("both-json")
    with TestClient(app) as client:
        res = client.post(
            f"/mcp/t/{token}",
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Origin": "https://grok.com",
            },
            json=INIT,
        )
        assert res.status_code == 200
        assert "application/json" in res.headers.get("content-type", "")
        assert res.json()["result"]["serverInfo"]["name"] == "TrendPilot"


def test_get_probe_is_200_not_405() -> None:
    token = mint_token("grok-build")
    with TestClient(app) as client:
        dead = client.get("/mcp", headers={"Origin": "https://grok.com"})
        assert dead.status_code == 401
        assert dead.json()["error"] == "Missing license key"
        live = client.get(
            f"/mcp/t/{token}",
            headers={"Accept": "*/*", "Origin": "https://grok.com"},
        )
        assert live.status_code == 200
        body = live.json()
        assert body["ok"] is True
        assert body["mcp"] is True
        assert body["transport"] == "streamable-http"
        assert body["server"]["name"] == "TrendPilot"
        assert body["first_tool"] == "onboard"
        head = client.head(f"/mcp/t/{token}")
        assert head.status_code == 200


def test_cors_preflight_from_grok() -> None:
    with TestClient(app) as client:
        res = client.options(
            "/mcp",
            headers={
                "Origin": "https://grok.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,authorization,accept",
            },
        )
        assert res.status_code == 200
        assert res.headers.get("access-control-allow-origin") == "*"
