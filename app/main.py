from __future__ import annotations

import asyncio
import hmac
import os
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.config import (
    CTA_ACCESS,
    CTA_LABEL,
    CTA_PRICE,
    PRICE_LABEL,
    PRICE_USD,
    PRODUCT_NAME,
    PUBLIC_DIR,
    STUDIO_NAME,
    TEMPLATES_DIR,
    THEORY_VIDEO_URL,
    admin_secret,
    data_dir,
    payment_mode,
    public_base_url,
    stripe_configured,
    stripe_payment_link,
)
from app.http_util import (
    AcceptCompatMiddleware,
    HttpsLocationMiddleware,
    InitializedCompatMiddleware,
    JsonContentTypeMiddleware,
    McpGetProbeMiddleware,
    NormalizeMcpPathMiddleware,
    TokenPathMiddleware,
    WellKnownRewriteMiddleware,
)
from app import automation
from app import order_guard
from app import run_guard
from app.automation import scheduler_loop
from app.mcp_server import bind_buyer, buyer_from_request, mcp
from app.media import buyer_media_dir, verify_media
from app.oauth import router as oauth_router, www_authenticate
from app.agent_discovery import (
    AgentDiscoveryHeadersMiddleware,
    checkout_not_configured_payload,
    order_agent_payload,
    payment_required_payload,
    router as agent_discovery_router,
)
from app.orders import fulfill_order
from app.stripe_checkout import (
    construct_webhook_event,
    create_checkout_session,
    metadata_from_session,
    retrieve_checkout_session,
    session_is_paid,
)
from app.proof import load_proof, proof_dir, render_proof_html
from app.store import ensure_buyer, list_buyers, list_leads
from app.tokens import clean_buyer_id, mint_token

PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

mcp_app = mcp.http_app(path="/", stateless_http=True, transport="streamable-http")
mcp_app.router.redirect_slashes = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp_app.lifespan(app):
        stop = asyncio.Event()
        task = asyncio.create_task(scheduler_loop(stop))
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class LicenseGate(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or "well-known" in path:
            return await call_next(request)
        buyer_id = buyer_from_request(request)
        if not buyer_id:
            return JSONResponse(
                {"error": "Missing license key"},
                status_code=401,
                headers={"WWW-Authenticate": www_authenticate(request)},
            )
        bind_buyer(buyer_id)
        request.state.buyer_id = buyer_id
        return await call_next(request)


mcp_app.add_middleware(LicenseGate)
mcp_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id", "mcp-session-id"],
)

app = FastAPI(title=PRODUCT_NAME, lifespan=lifespan, redirect_slashes=False)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.include_router(oauth_router)
app.include_router(agent_discovery_router)
app.mount("/assets", StaticFiles(directory=str(PUBLIC_DIR)), name="assets")
app.mount("/mcp", mcp_app)
app.add_middleware(HttpsLocationMiddleware)
app.add_middleware(McpGetProbeMiddleware)
app.add_middleware(InitializedCompatMiddleware)
app.add_middleware(AcceptCompatMiddleware)
app.add_middleware(JsonContentTypeMiddleware)
app.add_middleware(NormalizeMcpPathMiddleware)
app.add_middleware(TokenPathMiddleware)
app.add_middleware(WellKnownRewriteMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS", "HEAD"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id", "mcp-session-id", "WWW-Authenticate"],
)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")
app.add_middleware(AgentDiscoveryHeadersMiddleware)


def _ctx(request: Request, **extra):
    return {
        "request": request,
        "price": PRICE_USD,
        "price_label": PRICE_LABEL,
        "cta": CTA_LABEL,
        "access_cta": CTA_ACCESS,
        "price_cta": CTA_PRICE,
        "product": PRODUCT_NAME,
        "studio": STUDIO_NAME,
        "theory_url": THEORY_VIDEO_URL,
        **extra,
    }



def _admin_bypass(request: Request, body: dict) -> bool:
    """Bret-mediated mint: X-Admin-Secret or body admin_secret."""
    expected = admin_secret()
    if not expected:
        return False
    header = (request.headers.get("x-admin-secret") or "").strip()
    body_secret = str(body.get("admin_secret") or "").strip()
    if not header and not body_secret:
        return False
    ip = order_guard.client_ip(request)
    wait = order_guard.admin_locked(ip)
    if wait:
        raise _AdminLocked(wait)
    if header and hmac.compare_digest(header, expected):
        order_guard.admin_succeeded(ip)
        return True
    if body_secret and hmac.compare_digest(body_secret, expected):
        order_guard.admin_succeeded(ip)
        return True
    order_guard.admin_failed(ip)
    return False


class _AdminLocked(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("admin locked")
        self.retry_after = retry_after


def _secret_ok(candidate: str | None) -> bool:
    expected = admin_secret()
    if not expected or not candidate:
        return False
    return hmac.compare_digest(str(candidate), expected)


def _locked_json(retry_after: int) -> JSONResponse:
    return JSONResponse(
        {
            "ok": False,
            "error": "too_many_attempts",
            "message": "Too many wrong admin secrets. Try again later.",
            "retry_after": retry_after,
        },
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    content = request.headers.get("content-type", "")
    return "text/html" in accept or "application/x-www-form-urlencoded" in content


@app.get("/health")
async def health():
    root = data_dir()
    writable = False
    try:
        marker = root / ".writable"
        marker.write_text("ok", encoding="utf-8")
        writable = marker.exists()
    except OSError:
        writable = False
    return {
        "ok": True,
        "product": PRODUCT_NAME,
        "price": PRICE_USD,
        "data_dir": str(root),
        "data_dir_writable": writable,
        "persist": writable,
        "scheduler": automation.scheduler_started,
        "packs": {"run_guard": run_guard.PACK, "order_guard": order_guard.PACK},
        "run_guard": run_guard.summary(),
        "order_guard": order_guard.summary(),
    }


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request))


@app.head("/")
async def landing_head():
    """Agents/probes often HEAD the origin; return 200 + discovery Link headers."""
    return JSONResponse(content=None, status_code=200)


@app.get("/buy", response_class=HTMLResponse)
async def buy(request: Request):
    return templates.TemplateResponse(
        request,
        "buy.html",
        _ctx(request, payment_link=stripe_payment_link() or None),
    )


async def _read_body(request: Request) -> dict:
    if request.headers.get("content-type", "").startswith("application/json"):
        return await request.json()
    form = await request.form()
    return dict(form)


@app.post("/api/orders")
async def create_order(request: Request):
    body = await _read_body(request)
    email = str(body.get("email") or "").strip()
    name = str(body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A name is required.")
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A real email is required.")
    client = str(body.get("client") or "").strip()
    studio = str(body.get("studio") or "").strip()
    source = str(body.get("source") or "buy").strip() or "buy"
    wants_html = _wants_html(request)

    # Bret-mediated sales: admin secret mints immediately (no card).
    try:
        is_admin = _admin_bypass(request, body)
    except _AdminLocked as locked:
        return _locked_json(locked.retry_after)
    if is_admin:
        fulfilled = fulfill_order(
            name=name,
            email=email,
            client=client,
            studio=studio,
            source=f"admin:{source}",
            request=request,
        )
        if wants_html:
            return templates.TemplateResponse(
                request,
                "buy.html",
                _ctx(
                    request,
                    submitted=True,
                    order=fulfilled["order"],
                    mcp_url=fulfilled["url"],
                    mcp_token=fulfilled["token"],
                    payment_link=stripe_payment_link() or None,
                ),
            )
        return order_agent_payload(fulfilled)

    if not stripe_configured():
        payload = checkout_not_configured_payload()
        if wants_html:
            return templates.TemplateResponse(
                request,
                "buy.html",
                _ctx(
                    request,
                    submitted=False,
                    error=payload["message"],
                    payment_link=stripe_payment_link() or None,
                    payment_mode=payment_mode(),
                ),
                status_code=503,
            )
        return JSONResponse(payload, status_code=503)

    retry_after = order_guard.checkout_retry_after(order_guard.client_ip(request))
    if retry_after:
        message = "Too many checkout attempts from this connection. Try again later."
        if wants_html:
            return templates.TemplateResponse(
                request,
                "buy.html",
                _ctx(
                    request,
                    submitted=False,
                    error=message,
                    payment_link=stripe_payment_link() or None,
                    payment_mode=payment_mode(),
                ),
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        return JSONResponse(
            {
                "ok": False,
                "error": "rate_limited",
                "message": message,
                "retry_after": retry_after,
            },
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )

    try:
        session = create_checkout_session(
            name=name,
            email=email,
            client=client,
            studio=studio,
            source=source,
            request=request,
        )
    except Exception as exc:  # noqa: BLE001
        detail = f"Stripe Checkout could not be started: {exc}"
        if wants_html:
            return templates.TemplateResponse(
                request,
                "buy.html",
                _ctx(
                    request,
                    submitted=False,
                    error=detail,
                    payment_link=stripe_payment_link() or None,
                    payment_mode=payment_mode(),
                ),
                status_code=503,
            )
        return JSONResponse(
            {
                "ok": False,
                "payment_required": True,
                "error": "checkout_create_failed",
                "message": detail,
                "price_usd": PRICE_USD,
                "sku": "autopilot-mcp-license",
                "payment_mode": payment_mode(),
            },
            status_code=503,
        )

    checkout_url = session["checkout_url"]
    if wants_html:
        return RedirectResponse(checkout_url, status_code=303)
    return JSONResponse(
        payment_required_payload(
            checkout_url=checkout_url,
            buyer_id=session.get("buyer_id") or "",
            session_id=session.get("session_id") or "",
        ),
        status_code=402,
    )


@app.post("/api/leads")
async def create_lead_alias(request: Request):
    return await create_order(request)


@app.get("/api/orders/checkout")
async def checkout_redirect(session_id: str | None = Query(default=None)):
    """Redirect to Stripe's complete Checkout URL without touching its fragment."""
    if not session_id or not re.fullmatch(r"cs_(?:test|live)_[A-Za-z0-9]+", str(session_id)):
        return JSONResponse(
            {
                "ok": False,
                "error": "invalid_session_id",
                "message": "session_id (cs_test_... or cs_live_...) is required.",
            },
            status_code=400,
        )
    if not stripe_configured():
        return JSONResponse(
            {
                "ok": False,
                "error": "card_checkout_not_configured",
                "session_id": session_id,
            },
            status_code=503,
        )
    try:
        session = retrieve_checkout_session(session_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {
                "ok": False,
                "error": "checkout_session_not_found",
                "message": str(exc),
                "session_id": session_id,
            },
            status_code=404,
        )

    session_status = str(getattr(session, "status", None) or "")
    checkout_url = getattr(session, "url", None)
    if isinstance(session, dict):
        session_status = str(session.get("status") or "")
        checkout_url = session.get("url")
    if session_status.lower() == "expired":
        return JSONResponse(
            {
                "ok": False,
                "error": "checkout_session_expired",
                "message": "This Stripe Checkout Session has expired.",
                "session_id": session_id,
            },
            status_code=410,
        )
    if not isinstance(checkout_url, str) or not checkout_url:
        return JSONResponse(
            {
                "ok": False,
                "error": "checkout_session_unavailable",
                "message": "This Stripe Checkout Session has no active checkout URL.",
                "session_id": session_id,
            },
            status_code=410,
        )
    return RedirectResponse(checkout_url, status_code=302)


@app.get("/api/orders/status")
async def order_status(request: Request, session_id: str | None = Query(default=None)):
    """Agent poll after Checkout — JSON mcp_url once session is paid (and minted)."""
    if not session_id or not str(session_id).startswith("cs_"):
        raise HTTPException(status_code=400, detail="session_id (cs_...) is required.")
    poll_wait = order_guard.status_retry_after(order_guard.client_ip(request))
    if poll_wait:
        return JSONResponse(
            {
                "ok": False,
                "error": "rate_limited",
                "message": "Polling too fast. Wait and poll again.",
                "retry_after": poll_wait,
                "session_id": session_id,
            },
            status_code=429,
            headers={"Retry-After": str(poll_wait)},
        )
    if not stripe_configured():
        return JSONResponse(
            {
                "ok": False,
                "error": "card_checkout_not_configured",
                "payment_mode": payment_mode(),
                "session_id": session_id,
            },
            status_code=503,
        )
    try:
        session = retrieve_checkout_session(session_id)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {
                "ok": False,
                "error": "session_lookup_failed",
                "message": str(exc),
                "session_id": session_id,
            },
            status_code=400,
        )
    paid = session_is_paid(session)
    meta = metadata_from_session(session)
    email = (meta.get("email") or getattr(session, "customer_email", None) or "").strip()
    if not paid:
        payment_status = str(getattr(session, "payment_status", None) or "unpaid")
        session_status = str(getattr(session, "status", None) or "")
        return {
            "ok": True,
            "payment_status": payment_status,
            "session_status": session_status,
            "session_id": session_id,
            "buyer_id": meta.get("buyer_id") or None,
            "mcp_url": None,
            "payment_required": True,
            "price_usd": PRICE_USD,
            "sku": "autopilot-mcp-license",
            "message": "Payment not complete yet. Finish Checkout, then poll again.",
        }
    if not email or "@" not in email:
        return JSONResponse(
            {
                "ok": False,
                "error": "missing_buyer_email",
                "session_id": session_id,
                "payment_status": "paid",
            },
            status_code=400,
        )
    fulfilled = fulfill_order(
        name=(meta.get("name") or email or "Buyer").strip(),
        email=email,
        client=meta.get("client") or "",
        studio=meta.get("studio") or "",
        source=meta.get("source") or "stripe_status",
        request=request,
        session_id=str(session_id),
    )
    payload = order_agent_payload(fulfilled)
    payload["payment_status"] = "paid"
    payload["session_id"] = session_id
    return payload


@app.get("/admin", response_class=HTMLResponse)
async def admin_desk(request: Request, secret: str | None = Query(default=None)):
    cookie = request.cookies.get("admin")
    ip = order_guard.client_ip(request)
    authorized = _secret_ok(cookie)
    if not authorized and secret:
        wait = order_guard.admin_locked(ip)
        if wait:
            return _locked_json(wait)
        authorized = _secret_ok(secret)
        if authorized:
            order_guard.admin_succeeded(ip)
        else:
            order_guard.admin_failed(ip)
    response = templates.TemplateResponse(
        request,
        "admin.html",
        _ctx(
            request,
            authorized=authorized,
            buyers=list_buyers() if authorized else [],
            orders=list_leads() if authorized else [],
            minted=None,
        ),
        status_code=200 if authorized else 401,
    )
    if authorized:
        response.set_cookie("admin", admin_secret(), httponly=True, samesite="lax")
    return response


@app.post("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request, password: str = Form(...)):
    ip = order_guard.client_ip(request)
    wait = order_guard.admin_locked(ip)
    if wait:
        return _locked_json(wait)
    if not _secret_ok(password):
        order_guard.admin_failed(ip)
        return templates.TemplateResponse(
            request,
            "admin.html",
            _ctx(request, authorized=False),
            status_code=401,
        )
    order_guard.admin_succeeded(ip)
    response = RedirectResponse("/admin", status_code=303)
    response.set_cookie("admin", admin_secret(), httponly=True, samesite="lax")
    return response


@app.post("/admin/mint", response_class=HTMLResponse)
async def admin_mint(
    request: Request,
    buyer: str = Form(...),
    days: int = Form(0),
    note: str = Form(""),
):
    if not _secret_ok(request.cookies.get("admin")):
        raise HTTPException(status_code=401, detail="admin secret required")
    email = buyer if "@" in buyer else ""
    slug = clean_buyer_id(buyer.split("@", 1)[0])
    token = mint_token(slug, days=days)
    record = ensure_buyer(slug, email=email, note=note, token=token)
    url = f"{public_base_url()}/mcp/t/{record['token']}"
    return templates.TemplateResponse(
        request,
        "admin.html",
        _ctx(
            request,
            authorized=True,
            buyers=list_buyers(),
            orders=list_leads(),
            minted={
                "email": email,
                "buyer": buyer,
                "buyer_id": slug,
                "url": url,
                "price": PRICE_USD,
                "days": days or "none",
            },
        ),
    )


@app.get("/proof/{proof_id}", response_class=HTMLResponse)
async def proof_dashboard(proof_id: str):
    manifest = load_proof(proof_id)
    if not manifest:
        raise HTTPException(status_code=404, detail="Proof dashboard not found")
    return HTMLResponse(render_proof_html(manifest))


@app.get("/proof/{proof_id}/{filename}")
async def proof_shot(proof_id: str, filename: str):
    if not load_proof(proof_id):
        raise HTTPException(status_code=404, detail="Proof dashboard not found")
    name = Path(filename).name
    if not re.match(r"^[A-Za-z0-9._-]+\.(jpg|jpeg|png|webp|gif)$", name, re.I):
        raise HTTPException(status_code=404, detail="shot not found")
    path = proof_dir(proof_id) / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="shot not found")
    return FileResponse(path)


@app.get("/connected", response_class=HTMLResponse)
async def connected():
    return HTMLResponse(
        "<html><body style='background:#0c0f0d;color:#f3eee6;font-family:Manrope,sans-serif;padding:48px'>"
        "<p>Social connected on your PostProxy account. Return to Claude and call postproxy_status.</p>"
        "</body></html>"
    )


@app.get("/media/{buyer_id}/{filename}")
async def media(buyer_id: str, filename: str, sig: str = ""):
    if not verify_media(buyer_id, filename, sig):
        raise HTTPException(status_code=401, detail="signed media ticket required")
    path = buyer_media_dir(buyer_id) / Path(filename).name
    if not path.exists():
        raise HTTPException(status_code=404, detail="media not found")
    return FileResponse(path)


@app.get("/buy/thanks", response_class=HTMLResponse)
async def buy_thanks(request: Request, session_id: str | None = Query(default=None)):
    """After Stripe Checkout — show mcp_url only when session is paid."""
    if not session_id:
        return RedirectResponse("/buy", status_code=303)
    if not stripe_configured():
        return templates.TemplateResponse(
            request,
            "buy.html",
            _ctx(
                request,
                submitted=False,
                error="Card checkout is not configured; contact the studio for access.",
                payment_link=stripe_payment_link() or None,
            ),
            status_code=503,
        )
    try:
        session = retrieve_checkout_session(session_id)
    except Exception as exc:  # noqa: BLE001
        return templates.TemplateResponse(
            request,
            "buy.html",
            _ctx(
                request,
                submitted=False,
                error=f"Could not verify checkout session: {exc}",
                payment_link=stripe_payment_link() or None,
            ),
            status_code=400,
        )
    if not session_is_paid(session):
        return templates.TemplateResponse(
            request,
            "buy.html",
            _ctx(
                request,
                submitted=False,
                error="Payment is not complete yet. Finish Checkout, then refresh this page.",
                payment_link=stripe_payment_link() or None,
            ),
            status_code=402,
        )
    meta = metadata_from_session(session)
    email = (meta.get("email") or getattr(session, "customer_email", None) or "").strip()
    name = (meta.get("name") or email or "Buyer").strip()
    if not email or "@" not in email:
        return templates.TemplateResponse(
            request,
            "buy.html",
            _ctx(
                request,
                submitted=False,
                error="Paid session is missing buyer email metadata.",
                payment_link=stripe_payment_link() or None,
            ),
            status_code=400,
        )
    fulfilled = fulfill_order(
        name=name,
        email=email,
        client=meta.get("client") or "",
        studio=meta.get("studio") or "",
        source=meta.get("source") or "stripe",
        request=request,
    )
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept and "text/html" not in accept:
        payload = order_agent_payload(fulfilled)
        payload["payment_status"] = "paid"
        payload["session_id"] = session_id
        return JSONResponse(payload)
    return templates.TemplateResponse(
        request,
        "buy.html",
        _ctx(
            request,
            submitted=True,
            order=fulfilled["order"],
            mcp_url=fulfilled["url"],
            mcp_token=fulfilled["token"],
            payment_link=stripe_payment_link() or None,
        ),
    )


@app.get("/api/run-guard/summary")
async def run_guard_summary():
    """Backend pack status — no UI."""
    return run_guard.summary()


@app.post("/api/run-guard/check")
async def run_guard_check(request: Request):
    """Dry-run duplicate check for a proposed live publish."""
    body = await _read_body(request)
    return run_guard.check_publish(
        buyer_id=str(body.get("buyer_id") or body.get("id") or "probe"),
        platforms=body.get("platforms"),
        copy=body.get("copy") if isinstance(body.get("copy"), dict) else {
            "title": body.get("title") or "",
            "captions": body.get("captions") if isinstance(body.get("captions"), dict) else {},
        },
        media=body.get("media") if isinstance(body.get("media"), dict) else {
            "vertical_url": body.get("vertical_url") or "",
            "landscape_url": body.get("landscape_url") or "",
            "source_url": body.get("source_url") or "",
        },
        force=bool(body.get("force") or body.get("run_guard_force")),
    )


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """Verify Stripe signature; mint on checkout.session.completed."""
    payload = await request.body()
    signature = request.headers.get("stripe-signature")
    try:
        event = construct_webhook_event(payload, signature)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Webhook verify failed: {exc}") from exc
    etype = event["type"] if isinstance(event, dict) else event.type
    data = event["data"]["object"] if isinstance(event, dict) else event.data.object
    event_id = event.get("id") if isinstance(event, dict) else getattr(event, "id", None)
    if order_guard.seen_event(event_id):
        return {"ok": True, "received": True, "type": etype, "duplicate": True}
    if etype == "checkout.session.completed":
        meta = metadata_from_session(data)
        email = (meta.get("email") or getattr(data, "customer_email", None) or "").strip()
        name = (meta.get("name") or email or "Buyer").strip()
        if email and "@" in email:
            fulfill_order(
                name=name,
                email=email,
                client=meta.get("client") or "",
                studio=meta.get("studio") or "",
                source=meta.get("source") or "stripe_webhook",
                request=request,
                session_id=(
                    data.get("id") if isinstance(data, dict) else getattr(data, "id", None)
                ),
            )
    order_guard.mark_event(event_id)
    return {"ok": True, "received": True, "type": etype}


@app.get("/promo.mp4")
async def promo():
    path = PUBLIC_DIR / "promo.mp4"
    if not path.exists():
        raise HTTPException(status_code=404, detail="promo not rendered")
    return FileResponse(path, media_type="video/mp4")


@app.get("/poster.jpg")
async def poster():
    path = PUBLIC_DIR / "poster.jpg"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg")


@app.get("/og.jpg")
async def og():
    path = PUBLIC_DIR / "og.jpg"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/jpeg")



def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version="0.1.2",
        description=(
            f"{PRODUCT_NAME} — licensed MCP. Price {PRICE_LABEL}. "
            "Agents: GET /llms.txt, GET /api/pricing, POST /api/orders (402+checkout), "
            "GET /api/orders/checkout?session_id= to recover Stripe Checkout, then "
            "GET /api/orders/status?session_id=, then connect /mcp/t/{token}."
        ),
        routes=app.routes,
    )
    schema.setdefault("info", {})["x-sku"] = "autopilot-mcp-license"
    schema["info"]["x-price-usd"] = PRICE_USD
    paths = schema.setdefault("paths", {})
    orders = paths.get("/api/orders", {}).get("post")
    if orders is not None:
        orders["summary"] = "Start Stripe Checkout or admin-mint a licensed MCP URL"
        orders["description"] = (
            "POST JSON {name, email, source?} with Accept: application/json. "
            "Public callers get payment_required + checkout_url + checkout_redirect_url (402) when Stripe is configured; "
            "mcp_url is minted only after Checkout (webhook or /buy/thanks). "
            "X-Admin-Secret or admin_secret fulfills immediately. "
            "Without STRIPE_SECRET_KEY, public callers get 503 (admin mint only)."
        )
        orders["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["name", "email"],
                        "properties": {
                            "name": {"type": "string"},
                            "email": {"type": "string", "format": "email"},
                            "studio": {"type": "string"},
                            "client": {"type": "string"},
                            "source": {"type": "string", "default": "buy", "examples": ["agent", "buy"]},
                        },
                    }
                }
            },
        }
        orders["responses"] = {
            "200": {
                "description": "License minted (admin bypass only)",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "ok": {"type": "boolean"},
                                "sku": {"type": "string"},
                                "price_usd": {"type": "integer"},
                                "mcp_url": {"type": "string"},
                                "order_id": {"type": "string"},
                                "buyer_id": {"type": "string"},
                                "instructions": {"type": "object"},
                                "host_snippets": {"type": "object"},
                            },
                        }
                    }
                },
            },
            "402": {
                "description": "Payment required — Stripe Checkout URL + session_id",
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "properties": {
                                "ok": {"type": "boolean"},
                                "payment_required": {"type": "boolean"},
                                "checkout_url": {"type": "string", "format": "uri"},
                                "checkout_redirect_url": {"type": "string", "format": "uri"},
                                "session_id": {"type": "string"},
                                "status_url": {"type": "string"},
                                "price_usd": {"type": "integer"},
                                "sku": {"type": "string"},
                                "payment_mode": {"type": "string"},
                            },
                        }
                    }
                },
            },
            "400": {"description": "Missing name or email"},
            "503": {"description": "Card checkout not configured (admin mint only)"},
        }
    checkout = paths.get("/api/orders/checkout", {}).get("get")
    if checkout is not None:
        checkout["summary"] = "Recover the full Stripe Checkout URL"
        checkout["description"] = (
            "Retrieves the Checkout Session from Stripe and returns an exact 302 redirect "
            "to session.url. Returns JSON 404 for missing sessions and 410 for expired sessions."
        )
    status = paths.get("/api/orders/status", {}).get("get")
    if status is not None:
        status["summary"] = "Poll Checkout session until mcp_url is minted"
        status["description"] = (
            "GET ?session_id=cs_... after Stripe Checkout. Returns mcp_url when payment_status=paid."
        )
    pricing = paths.get("/api/pricing", {}).get("get")
    if pricing is not None:
        pricing["summary"] = "Machine-readable $997 one-time SKU"
        pricing["description"] = "Public pricing block for agents. Price stays $997 once."
    app.openapi_schema = schema
    return app.openapi_schema


app.openapi = custom_openapi


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        reload=False,
    )
