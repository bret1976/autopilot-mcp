from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send


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
