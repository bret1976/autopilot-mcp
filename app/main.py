from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

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
    public_base_url,
    stripe_payment_link,
)
from app.mcp_server import bind_buyer, buyer_from_request, mcp
from app.media import buyer_media_dir, verify_media
from app.store import append_lead, ensure_buyer, list_buyers, list_leads
from app.tokens import clean_buyer_id, mint_token

PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

mcp_app = mcp.http_app(path="/", stateless_http=True)


class LicenseGate(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        buyer_id = buyer_from_request(request)
        if not buyer_id:
            return JSONResponse({"error": "Missing license key"}, status_code=401)
        bind_buyer(buyer_id)
        request.state.buyer_id = buyer_id
        return await call_next(request)


mcp_app.add_middleware(LicenseGate)

app = FastAPI(title=PRODUCT_NAME, lifespan=mcp_app.lifespan)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.mount("/assets", StaticFiles(directory=str(PUBLIC_DIR)), name="assets")
app.mount("/mcp", mcp_app)


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


def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    content = request.headers.get("content-type", "")
    return "text/html" in accept or "application/x-www-form-urlencoded" in content


@app.get("/health")
async def health():
    return {"ok": True, "product": PRODUCT_NAME, "price": PRICE_USD}


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request, "index.html", _ctx(request))


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
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A real email is required.")
    order = append_lead(
        {
            "name": name,
            "email": email,
            "client": str(body.get("client") or "").strip(),
            "studio": str(body.get("studio") or "").strip(),
            "source": str(body.get("source") or "buy"),
        }
    )
    if _wants_html(request):
        return templates.TemplateResponse(
            request,
            "buy.html",
            _ctx(request, submitted=True, order=order, payment_link=stripe_payment_link() or None),
        )
    return {
        "ok": True,
        "message": "You are in line. Signed MCP link after payment.",
        "order_id": order["id"],
        "price": PRICE_USD,
    }


@app.post("/api/leads")
async def create_lead_alias(request: Request):
    return await create_order(request)


@app.get("/admin", response_class=HTMLResponse)
async def admin_desk(request: Request, secret: str | None = Query(default=None)):
    cookie = request.cookies.get("admin")
    authorized = secret == admin_secret() or cookie == admin_secret()
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
    if password != admin_secret():
        return templates.TemplateResponse(
            request,
            "admin.html",
            _ctx(request, authorized=False),
            status_code=401,
        )
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
    if request.cookies.get("admin") != admin_secret():
        raise HTTPException(status_code=401, detail="admin secret required")
    email = buyer if "@" in buyer else ""
    slug = clean_buyer_id(buyer.split("@", 1)[0])
    token = mint_token(slug, days=days)
    record = ensure_buyer(slug, email=email, note=note, token=token)
    url = f"{public_base_url()}/mcp?token={record['token']}"
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
async def buy_thanks():
    return RedirectResponse("/buy")


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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        reload=False,
    )
