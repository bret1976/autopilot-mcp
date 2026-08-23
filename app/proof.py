from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from PIL import Image, ImageDraw, ImageFont

from app.config import PRODUCT_NAME, data_dir, public_base_url
from app import postproxy
from app.platforms import normalize_platform

_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"RIFF", b"GIF8")
_LOGIN_MARKERS = (
    "log in",
    "sign in",
    "login",
    "create an account",
    "join linkedin",
    "log into facebook",
)
_OEMBED = {
    "youtube": "https://www.youtube.com/oembed?format=json&url={url}",
    "tiktok": "https://www.tiktok.com/oembed?url={url}",
    "twitter": "https://publish.twitter.com/oembed?url={url}",
    "x": "https://publish.twitter.com/oembed?url={url}",
}


def proofs_dir() -> Path:
    path = data_dir() / "proofs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def proof_dir(proof_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", proof_id)
    path = proofs_dir() / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def all_live_confirmed(posts: list[dict[str, Any]], *, draft: bool) -> bool:
    if draft or not posts:
        return False
    return all(bool(item.get("ok")) for item in posts)


def permalink_from(row: dict[str, Any]) -> str:
    for key in ("permalink", "url", "post_url", "link"):
        value = str(row.get(key) or "").strip()
        if value.startswith("http"):
            return value
    return ""


def _is_image(raw: bytes) -> bool:
    return bool(raw) and any(raw.startswith(magic) for magic in _IMAGE_MAGIC) and len(raw) > 4000


def _looks_like_login(html: str) -> bool:
    text = (html or "").lower()
    return any(marker in text for marker in _LOGIN_MARKERS) and len(text) < 80_000


async def wait_for_live_post(api_key: str, post_id: str, *, tries: int = 6) -> dict[str, Any]:
    last: dict[str, Any] = {}
    for attempt in range(tries):
        payload = await postproxy.get_post(api_key, post_id)
        if not isinstance(payload, dict):
            return last
        last = payload
        rows = postproxy.platform_outcomes(payload)
        if not rows:
            return last
        statuses = {str(row.get("status") or "").lower() for row in rows}
        if "failed" in statuses or "error" in statuses:
            return last
        if rows and all(
            str(row.get("status") or "").lower() == "published" and permalink_from(row)
            for row in rows
        ):
            return last
        if attempt + 1 < tries:
            await asyncio.sleep(2)
    return last


async def confirm_published_posts(
    api_key: str,
    posts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    confirmed: list[dict[str, Any]] = []
    for item in posts:
        row = dict(item)
        raw = row.get("result") if isinstance(row.get("result"), dict) else {}
        post_id = postproxy.result_post_id(raw)
        live = raw
        if api_key and post_id:
            try:
                live = await wait_for_live_post(api_key, post_id)
            except postproxy.PostProxyError:
                live = raw
        outcomes = postproxy.platform_outcomes(live) or postproxy.platform_outcomes(raw)
        permalink = ""
        status = "published" if row.get("ok") else "failed"
        for outcome in outcomes:
            permalink = permalink or permalink_from(outcome)
            if str(outcome.get("status") or "").strip():
                status = str(outcome.get("status") or status)
        permalink = permalink or permalink_from(raw) or permalink_from(row)
        row["post_id"] = post_id
        row["permalink"] = permalink
        row["live_status"] = status
        row["confirmed"] = bool(row.get("ok")) and status.lower() in {"published", "processed", "ok"}
        confirmed.append(row)
    return confirmed


async def _fetch_bytes(url: str, *, timeout: float = 12.0) -> bytes:
    headers = {"User-Agent": "TrendPilot-Proof/1.0"}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(url, headers=headers)
    if response.status_code >= 400:
        return b""
    return response.content


async def _oembed_image(platform: str, url: str) -> bytes:
    template = _OEMBED.get(normalize_platform(platform))
    if not template:
        return b""
    raw = await _fetch_bytes(template.format(url=quote(url, safe="")))
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return b""
    for key in ("thumbnail_url", "url"):
        image_url = str(payload.get(key) or "")
        if image_url.startswith("http") and "twitter.com" not in image_url:
            image = await _fetch_bytes(image_url)
            if _is_image(image):
                return image
    return b""


async def _og_image(url: str) -> bytes:
    raw = await _fetch_bytes(url)
    text = raw.decode("utf-8", "replace") if raw else ""
    if _looks_like_login(text):
        return b""
    match = re.search(
        r'<meta[^>]+(?:property|name)=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        text,
        re.I,
    )
    if not match:
        match = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']og:image["\']',
            text,
            re.I,
        )
    if not match:
        return b""
    image = await _fetch_bytes(match.group(1))
    return image if _is_image(image) else b""


async def _remote_screenshot(url: str) -> bytes:
    encoded = quote(url, safe="")
    candidates = [
        f"https://api.microlink.io/?url={encoded}&screenshot=true&meta=false&embed=screenshot.url",
        f"https://s.wordpress.com/mshots/v1/{encoded}?w=1200",
        f"https://image.thum.io/get/width/1200/noanimate/{url}",
    ]
    for candidate in candidates:
        raw = await _fetch_bytes(candidate, timeout=20.0)
        if candidate.startswith("https://api.microlink.io"):
            try:
                payload = json.loads(raw.decode("utf-8", "replace"))
                shot = str(((payload.get("data") or {}).get("screenshot") or {}).get("url") or "")
                if not shot:
                    continue
                raw = await _fetch_bytes(shot)
            except json.JSONDecodeError:
                continue
        if _is_image(raw):
            return raw
    return b""


def _write_receipt(path: Path, *, platform: str, account: str, permalink: str, poster: Path | None) -> None:
    canvas = Image.new("RGB", (1200, 675), (12, 15, 13))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    if poster and poster.exists():
        try:
            shot = Image.open(poster).convert("RGB")
            shot.thumbnail((640, 520))
            canvas.paste(shot, (40, 90))
        except Exception:  # noqa: BLE001
            pass
    draw.text((40, 28), f"CONFIRMED · {platform.upper()}", fill=(196, 165, 116), font=font)
    draw.text((40, 580), account[:80], fill=(243, 238, 230), font=font)
    draw.text((40, 610), (permalink or "Permalink pending")[:110], fill=(167, 162, 154), font=font)
    canvas.save(path, "JPEG", quality=88)


async def capture_platform_visual(
    *,
    platform: str,
    permalink: str,
    dest: Path,
    poster: Path | None = None,
) -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = ""
    raw = b""
    if permalink:
        raw = await _oembed_image(platform, permalink)
        source = "oembed" if raw else ""
        if not raw:
            raw = await _og_image(permalink)
            source = "og:image" if raw else ""
        if not raw:
            raw = await _remote_screenshot(permalink)
            source = "live_screenshot" if raw else ""
    if raw:
        dest.write_bytes(raw)
        return {"ok": True, "path": dest.name, "source": source, "permalink": permalink}
    _write_receipt(dest, platform=platform, account=platform, permalink=permalink, poster=poster)
    return {
        "ok": bool(permalink),
        "path": dest.name,
        "source": "receipt",
        "permalink": permalink,
        "note": "Live page blocked a screenshot. Showing the posted file plus the live permalink.",
    }


def load_proof(proof_id: str) -> dict[str, Any] | None:
    path = proof_dir(proof_id) / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _poster_path(media: dict[str, Any] | None) -> Path | None:
    if not media:
        return None
    for key in ("poster", "vertical", "landscape"):
        name = str(media.get(key) or "")
        if not name:
            continue
        candidate = Path(name)
        if candidate.exists():
            return candidate
    return None


def render_proof_html(manifest: dict[str, Any]) -> str:
    cards = []
    for item in manifest.get("platforms") or []:
        shot = escape(str(item.get("image") or ""))
        permalink = escape(str(item.get("permalink") or ""))
        platform = escape(str(item.get("platform") or "").title())
        account = escape(str(item.get("account") or ""))
        note = escape(str(item.get("note") or ""))
        live = "Live screenshot" if item.get("source") in {"live_screenshot", "oembed", "og:image"} else "Posted file + confirmation"
        link = (
            f'<a class="btn" href="{permalink}" target="_blank" rel="noreferrer">Open live {platform} post</a>'
            if permalink
            else "<p class='muted'>Permalink not public yet.</p>"
        )
        cards.append(
            f"""
            <article class="card shot">
              <p class="kicker">{platform} · {escape(str(item.get('live_status') or 'published'))}</p>
              <h3>{account or platform}</h3>
              <img src="{shot}" alt="Proof that {platform} was posted"/>
              <p class="muted">{live}</p>
              {f'<p class="fine">{note}</p>' if note else ''}
              {link}
            </article>
            """
        )
    brand = escape(str(manifest.get("brand_name") or "this brand"))
    when = escape(str(manifest.get("posted_at") or ""))
    title = escape(str(manifest.get("title") or "Autopilot cut"))
    count = len(manifest.get("platforms") or [])
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Posted · {brand}</title>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,500;1,500&family=Manrope:wght@400;500;600&display=swap"/>
  <link rel="stylesheet" href="/assets/page.css"/>
</head>
<body>
  <div class="shell proof">
    <p class="kicker">{escape(PRODUCT_NAME)} · visual proof</p>
    <h1>Posted on {count} socials</h1>
    <p class="lede">{brand} · {title}. Every network below is confirmed live. Open the screenshot, then the live post.</p>
    <p class="muted">{when}</p>
    <div class="proof-grid">
      {''.join(cards)}
    </div>
  </div>
</body>
</html>
"""


async def build_proof_dashboard(
    record: dict[str, Any],
    published: dict[str, Any],
    *,
    copy: dict[str, Any] | None = None,
    media: dict[str, Any] | None = None,
    draft: bool = False,
    capture=None,
) -> dict[str, Any] | None:
    posts = list(published.get("posts") or [])
    if not all_live_confirmed(posts, draft=draft):
        return None
    key = str(record.get("postproxy_api_key") or "")
    confirmed = await confirm_published_posts(key, posts)
    if not confirmed or not all(item.get("confirmed") for item in confirmed):
        return {
            "ok": False,
            "all_confirmed": False,
            "posts": confirmed,
            "say_to_user": "Not every social is confirmed live yet, so there is no proof link.",
        }
    proof_id = secrets.token_urlsafe(12)
    folder = proof_dir(proof_id)
    poster = _poster_path(media)
    capture_fn = capture or capture_platform_visual
    platforms: list[dict[str, Any]] = []
    for item in confirmed:
        platform = normalize_platform(str(item.get("platform") or "social"))
        image_name = f"{platform}.jpg"
        visual = await capture_fn(
            platform=platform,
            permalink=str(item.get("permalink") or ""),
            dest=folder / image_name,
            poster=poster,
        )
        platforms.append(
            {
                "platform": platform,
                "account": item.get("account") or platform,
                "permalink": item.get("permalink") or "",
                "live_status": item.get("live_status") or "published",
                "image": image_name,
                "source": visual.get("source"),
                "note": visual.get("note") or "",
            }
        )
    manifest = {
        "id": proof_id,
        "buyer_id": record.get("buyer_id"),
        "brand_name": record.get("brand_name") or "",
        "website_url": record.get("website_url") or "",
        "title": (copy or {}).get("title") or "",
        "posted_at": datetime.now(timezone.utc).isoformat(),
        "platforms": platforms,
        "all_confirmed": True,
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (folder / "index.html").write_text(render_proof_html(manifest), encoding="utf-8")
    url = f"{public_base_url().rstrip('/')}/proof/{proof_id}"
    names = ", ".join(item["platform"] for item in platforms)
    return {
        "ok": True,
        "all_confirmed": True,
        "id": proof_id,
        "url": url,
        "platforms": platforms,
        "say_to_user": (
            f"All {len(platforms)} socials are confirmed live ({names}). "
            f"Proof dashboard with screenshots: {url}"
        ),
    }


async def proof_from_last_run(record: dict[str, Any]) -> dict[str, Any]:
    last = record.get("last_run") or {}
    existing = (last.get("proof") or {}).get("url")
    if existing and (last.get("proof") or {}).get("ok"):
        return {
            "ok": True,
            "url": existing,
            "proof": last.get("proof"),
            "say_to_user": f"Proof dashboard: {existing}",
        }
    published = last.get("publish") or {}
    built = await build_proof_dashboard(
        record,
        published,
        copy=last.get("copy") or {},
        media=last.get("media") or {},
        draft=bool(last.get("draft")),
    )
    if not built:
        return {
            "ok": False,
            "say_to_user": "No confirmed live posts yet. Run Autopilot with draft=false first.",
        }
    return built
