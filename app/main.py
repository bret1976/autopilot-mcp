from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import (
    CTA_LABEL,
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
from app.tokens import mint_token

PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)

mcp_app = mcp.http_app(path="/", stateless_http=True)


class LicenseGate(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)
        buyer_id = buyer_from_request(request)
        if not buyer_id:
            return JSONResponse({"error": "license required"}, status_code=401)
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
        "product": PRODUCT_NAME,
        "studio": STUDIO_NAME,
        "theory_url": THEORY_VIDEO_URL,
        **extra,
    }


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


@app.post("/api/leads")
async def create_lead(request: Request):
    if request.headers.get("content-type", "").startswith("application/json"):
        body = await request.json()
    else:
        form = await request.form()
        body = dict(form)
    email = str(body.get("email") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A real email is required.")
    lead = append_lead(
        {
            "email": email,
            "studio": str(body.get("studio") or "").strip(),
            "note": str(body.get("note") or "").strip(),
            "source": str(body.get("source") or "buy"),
        }
    )
    if request.headers.get("accept", "").find("text/html") >= 0:
        return templates.TemplateResponse(
            request,
            "buy.html",
            _ctx(request, submitted=True, lead=lead, payment_link=stripe_payment_link() or None),
        )
    return {
        "ok": True,
        "message": "Bret will send your private MCP URL.",
        "lead_id": lead["id"],
        "price": PRICE_USD,
    }


@app.get("/admin", response_class=HTMLResponse)
async def admin_desk(
    request: Request,
    secret: str | None = Query(default=None),
):
    cookie = request.cookies.get("admin")
    if secret != admin_secret() and cookie != admin_secret():
        return templates.TemplateResponse(
            request,
            "admin.html",
            _ctx(request, authorized=False),
            status_code=401,
        )
    response = templates.TemplateResponse(
        request,
        "admin.html",
        _ctx(
            request,
            authorized=True,
            buyers=list_buyers(),
            leads=list_leads(),
            minted=None,
        ),
    )
    response.set_cookie("admin", admin_secret(), httponly=True, samesite="lax")
    return response


@app.post("/admin/mint", response_class=HTMLResponse)
async def admin_mint(
    request: Request,
    email: str = Form(...),
    buyer_id: str = Form(""),
    note: str = Form(""),
):
    if request.cookies.get("admin") != admin_secret():
        raise HTTPException(status_code=401, detail="admin secret required")
    slug = buyer_id.strip() or email.split("@", 1)[0]
    from app.tokens import clean_buyer_id

    slug = clean_buyer_id(slug)
    token = mint_token(slug)
    record = ensure_buyer(slug, email=email, note=note, token=token)
    url = f"{public_base_url()}/mcp?token={record['token']}"
    return templates.TemplateResponse(
        request,
        "admin.html",
        _ctx(
            request,
            authorized=True,
            buyers=list_buyers(),
            leads=list_leads(),
            minted={"email": email, "buyer_id": slug, "url": url, "price": PRICE_USD},
        ),
    )


@app.get("/connected", response_class=HTMLResponse)
async def connected(request: Request):
    return HTMLResponse(
        "<html><body style='background:#070707;color:#e8e6e3;font-family:sans-serif;padding:48px'>"
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
async def buy_thanks(request: Request):
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        reload=False,
    )
