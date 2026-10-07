from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import MAX_CLIP_SECONDS, data_dir, issuer_secret, public_base_url, ytdlp_cookies_file

SOURCE_BLOCK_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "login_required",
    "login required",
    "use --cookies",
    "http error 429",
    "http error 403",
    "unable to extract",
    "video unavailable",
    "private video",
    "members-only",
    "members only",
    "age-restricted",
    "join this channel",
    "requested content is not available",
    "rate-limit",
    "ratelimit",
    "captcha",
    "please log in",
    "please login",
    "cookies are no longer valid",
    "confirm you’re not a bot",
    "confirm you're not a bot",
)


# The YouTube bot wall is per-IP, not per-client: after three walls the rest of the
# player_client strategies fail the same way, so skip to the next non-YouTube original.
YOUTUBE_MAX_BOT_WALLS = 3


class MediaError(RuntimeError):
    def __init__(self, message: str, code: str = "download_failed") -> None:
        super().__init__(message)
        self.code = code


def is_source_block(text: str) -> bool:
    blob = (text or "").lower()
    return any(marker in blob for marker in SOURCE_BLOCK_MARKERS)


def source_block_message(url: str, detail: str = "") -> str:
    host = urlparse(url).netloc.replace("www.", "") or "the source host"
    tail = (detail or "").strip().splitlines()[-1][:240] if detail else ""
    extra = f" Host said: {tail}" if tail else ""
    return (
        f"Could not pull the source clip from {host}. "
        "That is the original-video download, not your YouTube channel or PostProxy publish."
        f"{extra}"
    )


YTDLP_TIMEOUT_SECONDS = 150
FFMPEG_TIMEOUT_SECONDS = 240


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    timeout = FFMPEG_TIMEOUT_SECONDS if "ffmpeg" in Path(cmd[0]).name else YTDLP_TIMEOUT_SECONDS
    try:
        return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")


def sign_media(buyer_id: str, filename: str, ttl: int = 60 * 60 * 12) -> str:
    exp = int(time.time()) + ttl
    msg = f"{buyer_id}/{filename}/{exp}"
    sig = hmac.new(issuer_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def verify_media(buyer_id: str, filename: str, ticket: str | None) -> bool:
    if not ticket or "." not in ticket:
        return False
    exp_raw, sig = ticket.split(".", 1)
    try:
        exp = int(exp_raw)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    msg = f"{buyer_id}/{filename}/{exp}"
    expected = hmac.new(issuer_secret().encode(), msg.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def media_url(buyer_id: str, filename: str, base_url: str | None = None) -> str:
    root = (base_url or public_base_url()).rstrip("/")
    ticket = sign_media(buyer_id, filename)
    return f"{root}/media/{buyer_id}/{filename}?sig={ticket}"


def buyer_media_dir(buyer_id: str) -> Path:
    path = data_dir() / "media" / buyer_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _which(name: str) -> str | None:
    return shutil.which(name)


def _is_youtube(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return any(part in host for part in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def is_youtube(url: str) -> bool:
    return _is_youtube(url or "")


DIRECT_VIDEO_EXTS = (".mp4", ".mov", ".m4v", ".webm")


def is_direct_video(url: str) -> bool:
    path = (urlparse(url or "").path or "").lower()
    return (url or "").startswith("http") and path.endswith(DIRECT_VIDEO_EXTS)


def source_kind(url: str) -> str:
    """Coarse source family. Datacenter-IP pull reliability: x > direct > tiktok/vimeo > reddit > youtube."""
    host = (urlparse(url or "").netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if _is_youtube(url or ""):
        return "youtube"
    if host in {"x.com", "twitter.com", "mobile.twitter.com", "mobile.x.com"} or host.endswith(".twitter.com"):
        return "x"
    if "reddit.com" in host or host in {"v.redd.it", "redd.it"}:
        return "reddit"
    if "tiktok.com" in host:
        return "tiktok"
    if "instagram.com" in host:
        return "instagram"
    if "vimeo.com" in host:
        return "vimeo"
    if is_direct_video(url):
        return "direct"
    return "other"


# Lower = tried first. YouTube is last: Railway's datacenter IP gets YouTube's
# "Sign in to confirm you're not a bot" wall on almost every pull.
SOURCE_PRIORITY = {
    "x": 0,
    "direct": 1,
    "vimeo": 2,
    "tiktok": 3,
    "reddit": 4,
    "other": 5,
    "instagram": 6,
    "youtube": 9,
}


def source_priority(url: str) -> int:
    return SOURCE_PRIORITY.get(source_kind(url), 5)


def is_post_url(url: str) -> bool:
    """True for a direct video/post permalink (not a search, profile, or home page)."""
    if not url or not url.startswith("http"):
        return False
    kind = source_kind(url)
    path = urlparse(url).path or ""
    if kind == "youtube":
        return "/shorts/" in path or "watch" in path or "youtu.be" in url
    if kind == "x":
        return "/status/" in path
    if kind == "reddit":
        return "/comments/" in path or "v.redd.it" in url
    if kind == "tiktok":
        return "/video/" in path
    if kind == "instagram":
        return any(seg in path for seg in ("/p/", "/reel/", "/reels/", "/tv/"))
    if kind == "vimeo":
        return any(ch.isdigit() for ch in path)
    if kind == "direct":
        return True
    return False


def impersonate_available() -> bool:
    try:
        import curl_cffi  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def _impersonate_failed(text: str) -> bool:
    blob = (text or "").lower()
    return any(
        marker in blob
        for marker in (
            "impersonate",
            "curl_cffi",
            "curl-cffi",
            "requested impersonate target",
        )
    )


def _pull_strategies(url: str) -> list[list[str]]:
    cookies = ytdlp_cookies_file()
    cookie_args = ["--cookies", str(cookies)] if cookies else []
    shared = ["--no-playlist", "--geo-bypass", "--socket-timeout", "30"]
    extras: list[list[str]] = []
    chrome = ["--impersonate", "chrome"] if impersonate_available() else []
    if _is_youtube(url):
        extras = [
            chrome + ["--extractor-args", "youtube:player_client=android,ios"],
            chrome + ["--extractor-args", "youtube:player_client=tv,web_safari"],
            ["--extractor-args", "youtube:player_client=android,ios"],
            ["--extractor-args", "youtube:player_client=tv,web_safari"],
            ["--extractor-args", "youtube:player_client=web_embedded,tv_embedded"],
            ["--extractor-args", "youtube:player_client=mweb,web"],
        ]
        if cookie_args:
            extras.insert(0, cookie_args + chrome + ["--extractor-args", "youtube:player_client=web,mweb,tv"])
    else:
        extras = []
        if chrome:
            extras.extend([["--impersonate", "chrome"], ["--impersonate", "safari"]])
        extras.append([])
        if cookie_args:
            extras.insert(0, cookie_args + chrome)
    cleaned: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for extra in extras:
        key = tuple(extra)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(shared + extra)
    return cleaned


def _find_raw(dest: Path, stem: str) -> Path | None:
    matches = [path for path in dest.glob(f"{stem}-raw.*") if path.is_file() and path.stat().st_size > 0]
    if not matches:
        return None
    return sorted(matches, key=lambda path: path.stat().st_mtime, reverse=True)[0]


def download_and_cut(
    buyer_id: str,
    source_url: str,
    *,
    start: float = 0.0,
    duration: float = MAX_CLIP_SECONDS,
    mock: bool = False,
) -> dict[str, Any]:
    duration = max(3.0, min(float(duration), float(MAX_CLIP_SECONDS)))
    dest = buyer_media_dir(buyer_id)
    stem = hashlib.sha256(source_url.encode()).hexdigest()[:12]
    raw_template = dest / f"{stem}-raw.%(ext)s"
    vertical = dest / f"{stem}-9x16.mp4"
    landscape = dest / f"{stem}-16x9.mp4"
    poster = dest / f"{stem}-poster.jpg"
    meta_path = dest / f"{stem}.json"

    if mock:
        raw = dest / f"{stem}-raw.mp4"
        for path in (raw, vertical, landscape, poster):
            path.write_bytes(b"MOCK")
        record = {
            "source_url": source_url,
            "mock": True,
            "duration": duration,
            "raw": raw.name,
            "vertical": vertical.name,
            "landscape": landscape.name,
            "poster": poster.name,
        }
        meta_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        return _public_record(buyer_id, record)

    yt_dlp = _which("yt-dlp")
    ffmpeg = _which("ffmpeg")
    if not yt_dlp:
        raise MediaError("yt-dlp is not installed on this host", code="downloader_missing")
    if not ffmpeg:
        raise MediaError("ffmpeg is not installed on this host", code="downloader_missing")

    last_err = ""
    raw: Path | None = None
    youtube_walls = 0
    for extra in _pull_strategies(source_url):
        if youtube_walls >= YOUTUBE_MAX_BOT_WALLS:
            break
        pull = _run(
            [
                yt_dlp,
                "-f",
                "bv*+ba/b",
                "--merge-output-format",
                "mp4",
                "-o",
                str(raw_template),
                *extra,
                source_url,
            ]
        )
        raw = _find_raw(dest, stem)
        if pull.returncode == 0 and raw is not None:
            break
        last_err = (pull.stderr or pull.stdout or "yt-dlp failed")[-800:]
        raw = None
        if _is_youtube(source_url) and is_source_block(last_err):
            youtube_walls += 1
        if _impersonate_failed(last_err) and not impersonate_available():
            continue

    if raw is None:
        if _impersonate_failed(last_err) and not impersonate_available():
            raise MediaError(
                "yt-dlp Chrome impersonation is missing on this host (curl_cffi). "
                + source_block_message(source_url, last_err),
                code="download_failed",
            )
        code = "source_bot_check" if is_source_block(last_err) else "download_failed"
        raise MediaError(source_block_message(source_url, last_err), code=code)

    _transcode(ffmpeg, raw, vertical, "1080:1920", start, duration)
    _transcode(ffmpeg, raw, landscape, "1920:1080", start, duration)
    _poster_frame(ffmpeg, landscape, poster)
    record = {
        "source_url": source_url,
        "mock": False,
        "duration": duration,
        "start": start,
        "raw": raw.name,
        "vertical": vertical.name,
        "landscape": landscape.name,
        "poster": poster.name,
    }
    meta_path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    return _public_record(buyer_id, record)


def _transcode(
    ffmpeg: str,
    source: Path,
    dest: Path,
    size: str,
    start: float,
    duration: float,
) -> None:
    vf = f"scale={size}:force_original_aspect_ratio=increase,crop={size},setsar=1"
    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-i",
        str(source),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(dest),
    ]
    result = _run(cmd)
    if result.returncode != 0 or not dest.exists():
        raise MediaError(result.stderr[-500:] or f"ffmpeg failed for {dest.name}", code="transcode_failed")


def _poster_frame(ffmpeg: str, source: Path, dest: Path) -> None:
    cmd = [
        ffmpeg,
        "-y",
        "-ss",
        "1",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        "scale=1200:900:force_original_aspect_ratio=increase,crop=1200:900,setsar=1",
        str(dest),
    ]
    result = _run(cmd)
    if result.returncode != 0 or not dest.exists():
        raise MediaError(result.stderr[-500:] or f"ffmpeg failed for {dest.name}", code="transcode_failed")


def _public_record(buyer_id: str, record: dict[str, Any]) -> dict[str, Any]:
    base = None
    stored = None
    try:
        from app.store import load_buyer

        stored = load_buyer(buyer_id)
        if stored:
            base = stored.get("public_base_url") or None
    except Exception:  # noqa: BLE001
        base = None
    urls = {
        **record,
        "vertical_url": media_url(buyer_id, record["vertical"], base),
        "landscape_url": media_url(buyer_id, record["landscape"], base),
    }
    if record.get("poster"):
        urls["poster_url"] = media_url(buyer_id, record["poster"], base)
    return urls


# --- Always-post fallback ----------------------------------------------------------
# When every scanned original is bot-walled or unreachable, a live "Autopost Right Now"
# must still publish. Order: operator-hosted clips (FALLBACK_CLIP_URLS, direct video
# links) → a short brand clip rendered on this host from the buyer's own website
# og:image. Never another company's clip.

FALLBACK_SECONDS = 12


def _fetch_bytes(url: str, timeout: float = 30.0, limit: int = 200 * 1024 * 1024) -> bytes:
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 TrendPilot-MCP/1.0"}) as client:
        response = client.get(url)
        response.raise_for_status()
        data = response.content
    if len(data) > limit:
        raise MediaError(f"fallback asset too large: {url}", code="download_failed")
    return data


def _has_video_stream(path: Path) -> bool:
    probe = shutil.which("ffprobe")
    if not probe:
        return path.exists() and path.stat().st_size > 0
    result = _run_ffprobe([probe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)])
    return bool((result.stdout or "").strip())


def _run_ffprobe(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "ffprobe timed out")


def cut_direct_clip(buyer_id: str, url: str, *, duration: float = MAX_CLIP_SECONDS) -> dict[str, Any]:
    """Fetch a direct .mp4/.mov/.webm over HTTPS (no yt-dlp) and cut both masters."""
    ffmpeg = _which("ffmpeg")
    if not ffmpeg:
        raise MediaError("ffmpeg is not installed on this host", code="downloader_missing")
    duration = max(3.0, min(float(duration), float(MAX_CLIP_SECONDS)))
    dest = buyer_media_dir(buyer_id)
    stem = hashlib.sha256(url.encode()).hexdigest()[:12]
    ext = Path(urlparse(url).path).suffix.lower() or ".mp4"
    raw = dest / f"{stem}-raw{ext if ext in DIRECT_VIDEO_EXTS else '.mp4'}"
    try:
        raw.write_bytes(_fetch_bytes(url))
    except MediaError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MediaError(f"Could not fetch fallback clip {url}: {exc}", code="download_failed") from exc
    if not _has_video_stream(raw):
        raise MediaError(f"Fallback clip has no video stream: {url}", code="download_failed")
    vertical = dest / f"{stem}-9x16.mp4"
    landscape = dest / f"{stem}-16x9.mp4"
    poster = dest / f"{stem}-poster.jpg"
    _transcode(ffmpeg, raw, vertical, "1080:1920", 0.0, duration)
    _transcode(ffmpeg, raw, landscape, "1920:1080", 0.0, duration)
    _poster_frame(ffmpeg, landscape, poster)
    record = {
        "source_url": url,
        "mock": False,
        "duration": duration,
        "start": 0.0,
        "raw": raw.name,
        "vertical": vertical.name,
        "landscape": landscape.name,
        "poster": poster.name,
        "media_source": "hosted_fallback",
    }
    (dest / f"{stem}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return _public_record(buyer_id, record)


def _brand_image(website: str, dest: Path) -> Path | None:
    """Download the brand's own og:image / twitter:image from its homepage."""
    import re
    from urllib.parse import urljoin

    if not website:
        return None
    try:
        html = _fetch_bytes(website, timeout=20.0, limit=5 * 1024 * 1024).decode("utf-8", "ignore")
    except Exception:  # noqa: BLE001
        return None
    found = ""
    for prop in ("og:image", "og:image:url", "twitter:image", "twitter:image:src"):
        for tag in re.findall(r"<meta[^>]+>", html, flags=re.I):
            if re.search(r"(?:property|name)\s*=\s*[\"']" + re.escape(prop) + r"[\"']", tag, flags=re.I):
                match = re.search(r"content\s*=\s*[\"']([^\"']+)[\"']", tag, flags=re.I)
                if match:
                    found = match.group(1).strip()
                    break
        if found:
            break
    if not found:
        return None
    image_url = urljoin(website, found.replace("&amp;", "&"))
    try:
        data = _fetch_bytes(image_url, timeout=20.0, limit=20 * 1024 * 1024)
    except Exception:  # noqa: BLE001
        return None
    path = dest / "brand-og-source.img"
    path.write_bytes(data)
    try:
        from PIL import Image

        with Image.open(path) as img:
            img.convert("RGB").save(dest / "brand-og.png")
        return dest / "brand-og.png"
    except Exception:  # noqa: BLE001
        return None


def _brand_card_image(dest: Path, brand: str, tagline: str) -> Path:
    """Plain brand card when the site has no og:image."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (1920, 1080), (10, 12, 18))
    draw = ImageDraw.Draw(img)
    try:
        big = ImageFont.load_default(size=150)
        small = ImageFont.load_default(size=56)
    except TypeError:  # very old Pillow
        big = small = ImageFont.load_default()
    draw.text((960, 470), brand or "", fill=(245, 245, 245), font=big, anchor="mm")
    if tagline:
        draw.text((960, 640), tagline[:70], fill=(170, 180, 200), font=small, anchor="mm")
    path = dest / "brand-card.png"
    img.save(path)
    return path


def render_brand_clip(buyer_id: str, record: dict[str, Any], *, seconds: int = FALLBACK_SECONDS) -> dict[str, Any]:
    """Render a short 9:16 + 16:9 brand clip from the buyer's own website art."""
    ffmpeg = _which("ffmpeg")
    if not ffmpeg:
        raise MediaError("ffmpeg is not installed on this host", code="downloader_missing")
    dest = buyer_media_dir(buyer_id)
    website = str(record.get("website_url") or "").strip()
    brand = str(record.get("brand_name") or "").strip()
    stem = "brand-" + hashlib.sha256(f"{website}|{brand}|{int(time.time())}".encode()).hexdigest()[:10]
    still = _brand_image(website, dest) or _brand_card_image(dest, brand, website.replace("https://", "").strip("/"))
    vertical = dest / f"{stem}-9x16.mp4"
    landscape = dest / f"{stem}-16x9.mp4"
    poster = dest / f"{stem}-poster.jpg"
    frames = int(seconds * 30)
    zoom = f"zoompan=z='min(zoom+0.0006,1.12)':d={frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':fps=30"
    common_tail = [
        "-f", "lavfi", "-t", str(seconds), "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
    ]
    out_tail = [
        "-map", "[v]", "-map", "1:a", "-t", str(seconds),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart",
    ]
    land_cmd = [
        ffmpeg, "-y", "-loop", "1", "-i", str(still), *common_tail,
        "-filter_complex",
        f"[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,{zoom}:s=1920x1080,format=yuv420p[v]",
        *out_tail, str(landscape),
    ]
    vert_cmd = [
        ffmpeg, "-y", "-loop", "1", "-i", str(still), *common_tail,
        "-filter_complex",
        "[0:v]split=2[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=30:2,eq=brightness=-0.15[bg];"
        "[b]scale=1080:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,{zoom}:s=1080x1920,format=yuv420p[v]",
        *out_tail, str(vertical),
    ]
    for cmd, path in ((land_cmd, landscape), (vert_cmd, vertical)):
        result = _run(cmd)
        if result.returncode != 0 or not path.exists():
            raise MediaError((result.stderr or "")[-500:] or f"ffmpeg failed for {path.name}", code="transcode_failed")
    _poster_frame(ffmpeg, landscape, poster)
    meta = {
        "source_url": website,
        "mock": False,
        "duration": float(seconds),
        "start": 0.0,
        "raw": still.name,
        "vertical": vertical.name,
        "landscape": landscape.name,
        "poster": poster.name,
        "media_source": "brand_fallback",
    }
    (dest / f"{stem}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return _public_record(buyer_id, meta)
