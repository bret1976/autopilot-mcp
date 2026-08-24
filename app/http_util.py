from __future__ import annotations

import json
import re
from urllib.parse import unquote

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_TOKEN_PATH = re.compile(r"^/mcp/t/([^/]+)(?P<rest>/.*)?$")


class TokenPathMiddleware:
    """Grok and some hosts strip ?token=. Keep the license in the path instead.

    /mcp/t/{token} is rewritten to /mcp/ with the token on the query and header.
    Hosts also GET /mcp/t/{token}/.well-known/... when adding a connector.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            path = scope.get("path") or ""
            match = _TOKEN_PATH.match(path)
            if match:
                token = unquote(match.group(1))
                rest = match.group("rest") or ""
                if rest.startswith("/.well-known/"):
                    new_path = rest
                else:
                    new_path = "/mcp/"
                scope = dict(scope)
                scope["path"] = new_path
                if "raw_path" in scope:
                    scope["raw_path"] = new_path.encode("latin-1")
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
    from app.config import MCP_SERVER_NAME, PRICE_USD

    return {
        "ok": True,
        "mcp": True,
        "jsonrpc": "2.0",
        "transport": "streamable-http",
        "protocol": "2024-11-05",
        "product": MCP_SERVER_NAME,
        "price": PRICE_USD,
        "server": {"name": MCP_SERVER_NAME, "title": MCP_SERVER_NAME},
        "allow": ["GET", "HEAD", "POST", "DELETE", "OPTIONS"],
        "hint": "This URL is a Streamable HTTP MCP server. POST JSON-RPC initialize here. GET without text/event-stream is a connector probe. First tool is onboard() with no website unless the user just pasted one.",
        "first_tool": "onboard",
        "brand": None,
        "setup": "clean",
        "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": MCP_SERVER_NAME, "title": MCP_SERVER_NAME},
        },
    }


def _original_accept(scope: Scope, request: Request) -> str:
    state = scope.get("state") or {}
    if "mcp_original_accept" in state:
        return str(state["mcp_original_accept"] or "")
    return request.headers.get("accept") or ""


def _wants_sse(scope: Scope, request: Request) -> bool:
    """Only hold GET open when the client is clearly an EventSource.

    Grok/ChatGPT probe with Accept: application/json, text/event-stream and
    wait for the GET to finish. A keep-alive stream makes them say the
    connector is unavailable. EventSource sends text/event-stream only, or
    reconnects with Last-Event-ID.
    """
    if request.headers.get("last-event-id"):
        return True
    accept = _original_accept(scope, request).lower()
    if "text/event-stream" not in accept:
        return False
    if "application/json" in accept or "*/*" in accept:
        return False
    return True


class McpGetProbeMiddleware:
    """Stateless FastMCP does not register GET. Other hosts still hit this URL.

    Licensed GET/HEAD probe (JSON, */*, or both) → 200 JSON, body closed.
    EventSource GET → a short finished SSE document (hosts time out if we hold it).
    JSON-RPC stays on POST. Never fall through to FastMCP 405. Never hang a GET.
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

        from app.oauth import www_authenticate
        from app.tokens import extract_token, verify_token

        parsed = verify_token(extract_token(request))
        if not parsed:
            body = json.dumps({"error": "Missing license key"}).encode()
            headers = [
                (b"allow", b"GET, HEAD, POST, DELETE, OPTIONS"),
                (b"cache-control", b"no-store"),
                (b"content-type", b"application/json"),
                (b"www-authenticate", www_authenticate(request).encode("latin-1")),
                (b"content-length", str(len(body)).encode()),
            ]
            await send({"type": "http.response.start", "status": 401, "headers": headers})
            await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})
            return

        if _wants_sse(scope, request):
            await _send_sse_keepalive(receive, send, method)
            return

        body = json.dumps(mcp_probe_payload()).encode()
        headers = [
            (b"allow", b"GET, HEAD, POST, DELETE, OPTIONS"),
            (b"cache-control", b"no-store"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": b"" if method == "HEAD" else body})


async def _send_sse_keepalive(receive: Receive, send: Send, method: str) -> None:
    headers = [
        (b"allow", b"GET, HEAD, POST, DELETE, OPTIONS"),
        (b"cache-control", b"no-cache, no-transform"),
        (b"connection", b"keep-alive"),
        (b"content-type", b"text/event-stream"),
        (b"x-accel-buffering", b"no"),
    ]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    if method == "HEAD":
        await send({"type": "http.response.body", "body": b""})
        return
    await send({"type": "http.response.body", "body": b": connected\n\n", "more_body": False})


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
        lowered = accept.lower()
        wants_sse = "text/event-stream" in lowered
        accepts_json = "application/json" in lowered or "*/*" in lowered or not lowered
        state = scope.setdefault("state", {})
        state["mcp_original_accept"] = accept
        state["mcp_wants_sse"] = wants_sse
        headers["accept"] = "application/json, text/event-stream"
        # Grok add-connector sends both Accept types then JSON.parses the body.
        # One-shot initialize is a single SSE event — unwrap it when JSON is allowed.
        prefer_json = accepts_json or not wants_sse

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


class JsonContentTypeMiddleware:
    """Some hosts POST JSON-RPC with no Content-Type or text/plain. FastMCP 415s."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and (scope.get("method") or "") == "POST":
            path = scope.get("path") or ""
            if path.startswith("/mcp"):
                headers = MutableHeaders(scope=scope)
                ctype = (headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                if ctype in {"", "text/plain"}:
                    headers["content-type"] = "application/json"
        await self.app(scope, receive, send)


_INITIALIZED = {
    "notifications/initialized",
    "initialized",
    "notification/initialized",
}


class InitializedCompatMiddleware:
    """Grok and a few custom connectors send notifications/initialized WITH an id.

    That is a request, not a notification. FastMCP then returns JSON-RPC -32602
    and the host aborts the handshake. Answer with an empty result instead.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (scope.get("method") or "") != "POST":
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or ""
        if not path.startswith("/mcp") or "well-known" in path:
            await self.app(scope, receive, send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body") or b"")
            if not message.get("more_body"):
                break

        try:
            payload = json.loads(bytes(body) or b"null")
        except json.JSONDecodeError:
            payload = None

        if isinstance(payload, dict):
            method = str(payload.get("method") or "")
            if method in _INITIALIZED and payload.get("id") is not None:
                reply = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": {}}).encode()
                headers = [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(reply)).encode()),
                    (b"cache-control", b"no-store"),
                ]
                await send({"type": "http.response.start", "status": 200, "headers": headers})
                await send({"type": "http.response.body", "body": reply})
                return

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


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
