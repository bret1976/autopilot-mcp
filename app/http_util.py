from __future__ import annotations

import json
import re
from urllib.parse import unquote

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_TOKEN_PATH = re.compile(r"^/mcp/t/([^/]+)/?$")


class TokenPathMiddleware:
    """Grok and some hosts strip ?token=. Keep the license in the path instead.

    /mcp/t/{token} is rewritten to /mcp/ with the token on the query and header.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            path = scope.get("path") or ""
            match = _TOKEN_PATH.match(path)
            if match:
                token = unquote(match.group(1))
                scope = dict(scope)
                scope["path"] = "/mcp/"
                if "raw_path" in scope:
                    scope["raw_path"] = b"/mcp/"
                extra = f"token={token}".encode("latin-1")
                existing = scope.get("query_string") or b""
                scope["query_string"] = extra if not existing else existing + b"&" + extra
                headers = list(scope.get("headers") or [])
                headers.append((b"x-mcp-token", token.encode("latin-1")))
                scope["headers"] = headers
        await self.app(scope, receive, send)


class WellKnownRewriteMiddleware:
    """Claude/Grok probe /mcp/.well-known/... — serve the parent well-known routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            path = scope.get("path") or ""
            if path.startswith("/mcp/.well-known/"):
                scope = dict(scope)
                rewritten = path[len("/mcp") :]
                scope["path"] = rewritten
                if "raw_path" in scope:
                    scope["raw_path"] = rewritten.encode("latin-1")
        await self.app(scope, receive, send)


class NormalizeMcpPathMiddleware:
    """Serve /mcp and /mcp/ as the same endpoint. No client-visible slash redirect."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"} and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            if "raw_path" in scope:
                scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


def mcp_probe_payload() -> dict:
    from app.config import PRODUCT_NAME, PRICE_USD

    return {
        "ok": True,
        "mcp": True,
        "jsonrpc": "2.0",
        "transport": "streamable-http",
        "protocol": "2024-11-05",
        "product": PRODUCT_NAME,
        "price": PRICE_USD,
        "server": {"name": "TrendPilot", "title": PRODUCT_NAME},
        "allow": ["GET", "HEAD", "POST", "DELETE", "OPTIONS"],
        "hint": "This URL is a Streamable HTTP MCP server. POST JSON-RPC initialize here. GET is only a connector probe.",
        "first_tool": "onboard",
    }


class McpGetProbeMiddleware:
    """Grok Build GETs the MCP URL first. Stateless FastMCP answers that with 405.

    A licensed GET/HEAD returns 200 discovery so the host does not treat the
    connector as dead. Real JSON-RPC stays on POST.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method") or ""
        path = scope.get("path") or ""
        if method not in {"GET", "HEAD"} or not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return
        if "well-known" in path or path.startswith("/mcp/media"):
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if request.headers.get("mcp-session-id"):
            await self.app(scope, receive, send)
            return

        from app.oauth import www_authenticate
        from app.tokens import extract_token, verify_token

        parsed = verify_token(extract_token(request))
        headers = [
            (b"allow", b"GET, HEAD, POST, DELETE, OPTIONS"),
            (b"cache-control", b"no-store"),
        ]
        if not parsed:
            body = json.dumps({"error": "Missing license key"}).encode()
            headers.extend(
                [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", www_authenticate(request).encode("latin-1")),
                    (b"content-length", str(len(body)).encode()),
                ]
            )
            await send({"type": "http.response.start", "status": 401, "headers": headers})
            await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})
            return

        body = json.dumps(mcp_probe_payload()).encode()
        headers.extend(
            [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ]
        )
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})


class AcceptCompatMiddleware:
    """Grok and some browsers send Accept: application/json or */*.

    FastMCP 406s unless both application/json and text/event-stream are listed.
    Widen Accept so the handshake runs. If the client did not ask for SSE,
    unwrap the one-shot SSE initialize into JSON.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        headers = MutableHeaders(scope=scope)
        accept = headers.get("accept", "")
        wants_sse = "text/event-stream" in accept
        headers["accept"] = "application/json, text/event-stream"
        prefer_json = not wants_sse

        if not prefer_json:
            await self.app(scope, receive, send)
            return

        start: dict | None = None
        body = bytearray()

        async def capture(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
                return
            if message["type"] == "http.response.body":
                body.extend(message.get("body") or b"")
                if message.get("more_body"):
                    return
                assert start is not None
                await _send_maybe_json(send, start, bytes(body))

        await self.app(scope, receive, capture)


async def _send_maybe_json(send: Send, start: Message, body: bytes) -> None:
    headers = MutableHeaders(raw=list(start.get("headers") or []))
    ctype = headers.get("content-type", "")
    payload = body
    if "text/event-stream" in ctype:
        extracted = _first_sse_data(body)
        if extracted is not None:
            payload = extracted
            headers["content-type"] = "application/json"
            headers["content-length"] = str(len(payload))
            if "cache-control" in headers:
                del headers["cache-control"]
            if "x-accel-buffering" in headers:
                del headers["x-accel-buffering"]
    await send(
        {
            "type": "http.response.start",
            "status": start.get("status", 200),
            "headers": headers.raw,
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _first_sse_data(body: bytes) -> bytes | None:
    text = body.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("data:"):
            return line[5:].lstrip().encode("utf-8")
    return None


class HttpsLocationMiddleware:
    """If a proxy still emits a slash redirect, keep https and the token query."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_https(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = []
                for key, value in message.get("headers", []):
                    if key.lower() == b"location":
                        location = value.decode("latin-1")
                        if location.startswith("http://"):
                            location = "https://" + location[len("http://") :]
                        value = location.encode("latin-1")
                    headers.append((key, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_https)
