from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import MCP_SERVER_NAME, data_dir, public_base_url
from app.tokens import token_from_paste, verify_token

router = APIRouter(tags=["oauth"])

CODE_TTL_SECONDS = 300
SCOPES = ["mcp:tools", "openid"]


def request_base(request: Request | None = None) -> str:
    if request is None:
        return public_base_url()
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https")
    proto = proto.split(",")[0].strip() or "https"
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    host = host.split(",")[0].strip()
    if host:
        return f"{proto}://{host}".rstrip("/")
    return public_base_url()


def resource_url(request: Request | None = None) -> str:
    return f"{request_base(request)}/mcp"


def www_authenticate(request: Request | None = None) -> str:
    meta = f"{request_base(request)}/.well-known/oauth-protected-resource"
    return f'Bearer realm="{MCP_SERVER_NAME}", resource_metadata="{meta}"'


def _codes_path():
    path = data_dir() / "oauth_codes.json"
    if not path.exists():
        path.write_text("{}", encoding="utf-8")
    return path


def _load_codes() -> dict[str, Any]:
    try:
        return json.loads(_codes_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_codes(payload: dict[str, Any]) -> None:
    now = int(time.time())
    live = {key: value for key, value in payload.items() if int(value.get("exp") or 0) >= now}
    _codes_path().write_text(json.dumps(live), encoding="utf-8")


def _pkce_s256(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _redirect_ok(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    if parsed.scheme == "http" and (parsed.hostname or "") in {"localhost", "127.0.0.1"}:
        return True
    return False


def protected_resource_metadata(request: Request) -> dict[str, Any]:
    from app.tokens import extract_token, verify_token

    base = request_base(request)
    token = extract_token(request)
    parsed = verify_token(token) if token else None
    default = f"{base}/mcp/t/{parsed.raw}" if parsed else f"{base}/mcp"
    resource = request.query_params.get("resource") or default
    payload = {
        "resource": resource,
        "bearer_methods_supported": ["header", "query"],
        "scopes_supported": SCOPES,
        "resource_documentation": f"{base}/",
    }
    # Path-token URLs already carry the license. Advertising an authorization
    # server makes Grok start OAuth, fail the paste form, and show Connection failed.
    if not parsed:
        payload["authorization_servers"] = [base]
    return payload


def authorization_server_metadata(request: Request) -> dict[str, Any]:
    base = request_base(request)
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "scopes_supported": SCOPES,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256", "plain"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
        "revocation_endpoint": f"{base}/oauth/revoke",
    }


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/{rest:path}")
async def oauth_protected_resource(request: Request):
    return JSONResponse(protected_resource_metadata(request))


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/oauth-authorization-server/{rest:path}")
@router.get("/.well-known/openid-configuration")
@router.get("/.well-known/openid-configuration/{rest:path}")
async def oauth_authorization_server(request: Request):
    return JSONResponse(authorization_server_metadata(request))


@router.post("/oauth/register")
async def oauth_register(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    client_id = secrets.token_urlsafe(24)
    redirect_uris = body.get("redirect_uris") or []
    if isinstance(redirect_uris, str):
        redirect_uris = [redirect_uris]
    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "client_secret_expires_at": 0,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": body.get("token_endpoint_auth_method") or "none",
            "grant_types": body.get("grant_types") or ["authorization_code"],
            "response_types": body.get("response_types") or ["code"],
            "client_name": body.get("client_name") or MCP_SERVER_NAME,
        },
        status_code=201,
    )


def _authorize_html(request: Request, error: str = "") -> HTMLResponse:
    client_id = request.query_params.get("client_id") or ""
    redirect_uri = request.query_params.get("redirect_uri") or ""
    state = request.query_params.get("state") or ""
    code_challenge = request.query_params.get("code_challenge") or ""
    code_challenge_method = request.query_params.get("code_challenge_method") or "S256"
    scope = request.query_params.get("scope") or "mcp:tools"
    resource = request.query_params.get("resource") or resource_url(request)
    err = error or request.query_params.get("error") or ""
    err_html = f'<p class="err">{html.escape(err)}</p>' if err else ""

    def field(name: str, value: str) -> str:
        return f'<input type="hidden" name="{name}" value="{html.escape(value, quote=True)}"/>'

    return HTMLResponse(
        f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Connect {MCP_SERVER_NAME}</title>
<link rel="stylesheet" href="/assets/page.css"/>
</head>
<body>
<div class="shell buy">
  <p class="kicker">{MCP_SERVER_NAME}</p>
  <h1>Connect your license</h1>
  <p class="lede">Grok, Claude, ChatGPT, and other hosts ask us to confirm the same signed URL you already have. Paste the MCP link or the token after <code>token=</code>.</p>
  {err_html}
  <form class="card" method="post" action="/oauth/authorize">
    {field("client_id", client_id)}
    {field("redirect_uri", redirect_uri)}
    {field("state", state)}
    {field("code_challenge", code_challenge)}
    {field("code_challenge_method", code_challenge_method)}
    {field("scope", scope)}
    {field("resource", resource)}
    <label for="license">MCP URL or license token</label>
    <input id="license" name="license" type="text" required autocomplete="off" placeholder="https://…/mcp/t/… or the token"/>
    <button class="btn big" type="submit">Connect</button>
  </form>
</div>
</body></html>"""
    )


def _authorize_hint(request: Request) -> str | None:
    """Grok starts OAuth with resource=https://host/mcp/t/TOKEN and no login form."""
    return token_from_paste(
        request.query_params.get("login_hint")
        or request.query_params.get("token")
        or request.query_params.get("license")
        or request.query_params.get("resource")
        or request.headers.get("x-mcp-token")
        or request.url.path
    )


@router.get("/oauth/authorize")
async def oauth_authorize_form(request: Request):
    hint = _authorize_hint(request)
    if hint and verify_token(hint) and request.query_params.get("redirect_uri"):
        return _issue_code_redirect(
            license_token=hint,
            redirect_uri=request.query_params.get("redirect_uri") or "",
            state=request.query_params.get("state") or "",
            code_challenge=request.query_params.get("code_challenge") or "",
            code_challenge_method=request.query_params.get("code_challenge_method") or "S256",
            client_id=request.query_params.get("client_id") or "",
        )
    return _authorize_html(request)


@router.post("/oauth/authorize")
async def oauth_authorize_submit(
    request: Request,
    license: str = Form(""),
    redirect_uri: str = Form(""),
    state: str = Form(""),
    code_challenge: str = Form(""),
    code_challenge_method: str = Form("S256"),
    client_id: str = Form(""),
):
    token = token_from_paste(license)
    if not token or not verify_token(token):
        return _authorize_html(request, error="That license is not valid. Paste the full MCP URL you were emailed.")
    return _issue_code_redirect(
        license_token=token,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        client_id=client_id,
    )


def _issue_code_redirect(
    *,
    license_token: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    client_id: str,
) -> RedirectResponse:
    if not _redirect_ok(redirect_uri):
        raise HTTPException(status_code=400, detail="redirect_uri must be https (or localhost)")
    code = secrets.token_urlsafe(32)
    codes = _load_codes()
    codes[code] = {
        "token": license_token,
        "exp": int(time.time()) + CODE_TTL_SECONDS,
        "challenge": code_challenge,
        "method": (code_challenge_method or "S256").upper(),
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }
    _save_codes(codes)
    query = {"code": code}
    if state:
        query["state"] = state
    joiner = "&" if urlparse(redirect_uri).query else "?"
    return RedirectResponse(f"{redirect_uri}{joiner}{urlencode(query)}", status_code=302)


@router.post("/oauth/token")
async def oauth_token(request: Request):
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    code = str(body.get("code") or "")
    verifier = str(body.get("code_verifier") or "")
    redirect_uri = str(body.get("redirect_uri") or "")
    codes = _load_codes()
    record = codes.pop(code, None)
    _save_codes(codes)
    if not record:
        raise HTTPException(status_code=400, detail="invalid_grant")
    if int(record.get("exp") or 0) < int(time.time()):
        raise HTTPException(status_code=400, detail="invalid_grant")
    if redirect_uri and record.get("redirect_uri") and redirect_uri != record["redirect_uri"]:
        raise HTTPException(status_code=400, detail="invalid_grant")
    challenge = record.get("challenge") or ""
    method = (record.get("method") or "S256").upper()
    if challenge:
        if not verifier:
            raise HTTPException(status_code=400, detail="invalid_grant")
        expected = _pkce_s256(verifier) if method == "S256" else verifier
        if expected != challenge:
            raise HTTPException(status_code=400, detail="invalid_grant")
    token = str(record["token"])
    parsed = verify_token(token)
    expires_in = 365 * 86400
    if parsed and parsed.expires_at:
        expires_in = max(60, parsed.expires_at - int(time.time()))
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "scope": " ".join(SCOPES),
    }


@router.post("/oauth/revoke")
async def oauth_revoke():
    return JSONResponse({}, status_code=200)
