#!/usr/bin/env python3
"""Live host-shaped audit. Set MCP_AUDIT_URL to the path license URL."""
from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

HOST = os.environ.get("MCP_AUDIT_HOST", "https://autopilot-mcp-production-c6f7.up.railway.app").rstrip("/")
TOKEN = os.environ.get("MCP_AUDIT_TOKEN", "")
PATH = os.environ.get("MCP_AUDIT_URL", "")
if not PATH and TOKEN:
    PATH = f"{HOST}/mcp/t/{TOKEN}"

INITS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")


def req(method: str, url: str, *, headers=None, body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    started = time.time()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read(), time.time() - started
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read(), time.time() - started
    except Exception as exc:  # noqa: BLE001
        return 0, {}, str(exc).encode(), time.time() - started


def parse(raw: bytes):
    text = raw.decode("utf-8", "replace")
    if text.startswith("event:") or text.startswith(":") or "data:" in text[:80]:
        for line in text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return {"_sse": text[:200]}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text[:300]}


def rpc(method: str, params=None, *, accept="application/json", proto="2024-11-05", extra=None, has_id=True):
    headers = {
        "Accept": accept,
        "Content-Type": "application/json",
        "MCP-Protocol-Version": proto,
    }
    if extra:
        headers.update(extra)
    payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if has_id:
        payload["id"] = 1
    return req("POST", PATH, headers=headers, body=payload)


def main() -> int:
    if not PATH:
        print("Set MCP_AUDIT_URL or MCP_AUDIT_TOKEN", file=sys.stderr)
        return 2

    rows = []
    failed = []

    def note(name: str, ok: bool, status: int, elapsed: float, detail: str) -> None:
        mark = "PASS" if ok else "FAIL"
        rows.append((mark, name, status, elapsed, detail))
        print(f"{mark:4} {name:44} {status:>4} {elapsed:5.2f}s  {detail}")
        if not ok:
            failed.append(name)

    status, headers, raw, elapsed = req("GET", f"{HOST}/health")
    health = parse(raw)
    note(
        "GET /health",
        status == 200 and health.get("data_dir_writable") is True,
        status,
        elapsed,
        f"dir={health.get('data_dir')} writable={health.get('data_dir_writable')}",
    )

    status, _, raw, elapsed = req("GET", f"{HOST}/mcp")
    note("GET /mcp no token", status == 401, status, elapsed, parse(raw).get("error", raw[:60]))

    status, headers, raw, elapsed = req("GET", PATH, headers={"Accept": "*/*", "Origin": "https://grok.com"})
    body = parse(raw)
    note(
        "GET probe */* (Grok Build)",
        status == 200 and body.get("server", {}).get("name") == "TrendPilot",
        status,
        elapsed,
        f"ok={body.get('ok')} ctype={headers.get('Content-Type', '')[:40]}",
    )

    started = time.time()
    parsed = urlparse(PATH)
    conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=6)
    try:
        conn.request(
            "GET",
            parsed.path + (f"?{parsed.query}" if parsed.query else ""),
            headers={"Accept": "application/json, text/event-stream", "Mcp-Session-Id": "stale"},
        )
        resp = conn.getresponse()
        chunk = resp.read(13)
        sse_status, sse_ctype = resp.status, resp.getheader("Content-Type") or ""
    except Exception as exc:  # noqa: BLE001
        sse_status, sse_ctype, chunk = 0, "", str(exc).encode()
    finally:
        conn.close()
    note(
        "GET SSE + stale session",
        sse_status == 200 and "text/event-stream" in sse_ctype,
        sse_status,
        time.time() - started,
        f"ctype={sse_ctype[:48]} body={chunk[:40]!r}",
    )

    status, headers, raw, elapsed = req("HEAD", PATH)
    note("HEAD path token", status == 200, status, elapsed, f"allow={headers.get('Allow') or headers.get('allow')}")

    status, headers, raw, elapsed = req(
        "OPTIONS",
        PATH,
        headers={
            "Origin": "https://grok.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,accept,mcp-protocol-version",
        },
    )
    acao = headers.get("Access-Control-Allow-Origin") or headers.get("access-control-allow-origin")
    note(
        "OPTIONS CORS",
        status == 200 and acao == "*",
        status,
        elapsed,
        f"acao={acao}",
    )

    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
        "/mcp/.well-known/oauth-protected-resource",
    ):
        status, _, raw, elapsed = req("GET", HOST + path)
        note(f"GET {path[-42:]}", status == 200, status, elapsed, raw[:50].decode("utf-8", "replace"))

    for proto in INITS:
        status, headers, raw, elapsed = rpc(
            "initialize",
            {"protocolVersion": proto, "capabilities": {}, "clientInfo": {"name": "audit", "version": "0"}},
            proto=proto,
        )
        data = parse(raw)
        name = ((data.get("result") or {}).get("serverInfo") or {}).get("name")
        note(f"initialize {proto}", status == 200 and name == "TrendPilot", status, elapsed, f"name={name}")

    for accept, label in (
        ("application/json", "json-only"),
        ("*/*", "star"),
        ("application/json, text/event-stream", "both"),
        ("text/event-stream", "sse-only"),
    ):
        status, _, raw, elapsed = rpc(
            "initialize",
            {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "a", "version": "0"}},
            accept=accept,
        )
        name = ((parse(raw).get("result") or {}).get("serverInfo") or {}).get("name")
        note(f"initialize Accept {label}", status == 200 and name == "TrendPilot", status, elapsed, f"name={name}")

    status, _, raw, elapsed = rpc("notifications/initialized", None, has_id=False)
    note("initialized (notification)", status in {200, 202}, status, elapsed, raw[:80].decode("utf-8", "replace"))

    status, _, raw, elapsed = rpc("notifications/initialized", None, has_id=True)
    data = parse(raw)
    note(
        "initialized WITH id",
        status == 200 and "error" not in data,
        status,
        elapsed,
        f"error={data.get('error')} result={data.get('result')}",
    )

    status, _, raw, elapsed = rpc("tools/list", {})
    data = parse(raw)
    tools = [t.get("name") for t in ((data.get("result") or {}).get("tools") or [])]
    note("tools/list", status == 200 and "onboard" in tools, status, elapsed, f"n={len(tools)}")

    status, _, raw, elapsed = rpc("tools/call", {"name": "onboard", "arguments": {}})
    data = parse(raw)
    result = data.get("result") or {}
    structured = result.get("structuredContent") or {}
    content = result.get("content") or []
    note(
        "tools/call onboard",
        status == 200 and (structured or content),
        status,
        elapsed,
        f"needs_setup={structured.get('needs_setup')} content={bool(content)} missing={structured.get('missing')}",
    )

    status, _, raw, elapsed = rpc("tools/call", {"name": "status", "arguments": {}})
    data = parse(raw)
    structured = (data.get("result") or {}).get("structuredContent") or {}
    note(
        "tools/call status",
        status == 200 and (structured.get("busy") is False or structured.get("job") is not None or "missing" in structured),
        status,
        elapsed,
        f"busy={structured.get('busy')} ready={structured.get('ready')} missing={structured.get('missing')}",
    )

    status, _, raw, elapsed = rpc("ping", {})
    note("ping", status == 200 and "error" not in parse(raw), status, elapsed, raw[:80].decode("utf-8", "replace"))

    status, _, raw, elapsed = req(
        "POST",
        f"{HOST}/mcp?token={TOKEN}" if TOKEN else PATH,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "q", "version": "0"}},
        },
    )
    name = ((parse(raw).get("result") or {}).get("serverInfo") or {}).get("name")
    note("query-token initialize", status == 200 and name == "TrendPilot", status, elapsed, f"name={name}")

    if TOKEN:
        status, _, raw, elapsed = req(
            "POST",
            f"{HOST}/mcp",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TOKEN}",
            },
            body={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "b", "version": "0"},
                },
            },
        )
        name = ((parse(raw).get("result") or {}).get("serverInfo") or {}).get("name")
        note("Bearer initialize", status == 200 and name == "TrendPilot", status, elapsed, f"name={name}")

    print()
    print("TOOLS:", tools)
    if failed:
        print("FAILED:", ", ".join(failed))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
