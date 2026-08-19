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


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


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


def _pull_strategies(url: str) -> list[list[str]]:
    cookies = ytdlp_cookies_file()
    cookie_args = ["--cookies", str(cookies)] if cookies else []
    shared = ["--no-playlist", "--geo-bypass", "--socket-timeout", "30"]
    extras: list[list[str]] = []
    if _is_youtube(url):
        extras = [
            ["--extractor-args", "youtube:player_client=tv,web_safari"],
            ["--extractor-args", "youtube:player_client=web_embedded,tv_embedded"],
            ["--extractor-args", "youtube:player_client=mweb,web"],
            ["--impersonate", "chrome", "--extractor-args", "youtube:player_client=tv,web_safari"],
        ]
        if cookie_args:
            extras.insert(0, cookie_args + ["--extractor-args", "youtube:player_client=web,mweb,tv"])
    else:
        extras = [
            ["--impersonate", "chrome"],
            [],
        ]
        if cookie_args:
            extras.insert(0, cookie_args)
    return [shared + extra for extra in extras]


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
    meta_path = dest / f"{stem}.json"

    if mock:
        raw = dest / f"{stem}-raw.mp4"
        for path in (raw, vertical, landscape):
            path.write_bytes(b"MOCK")
        record = {
            "source_url": source_url,
            "mock": True,
            "duration": duration,
            "raw": raw.name,
            "vertical": vertical.name,
            "landscape": landscape.name,
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
    for extra in _pull_strategies(source_url):
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

    if raw is None:
        code = "source_bot_check" if is_source_block(last_err) else "download_failed"
        raise MediaError(source_block_message(source_url, last_err), code=code)

    _transcode(ffmpeg, raw, vertical, "1080:1920", start, duration)
    _transcode(ffmpeg, raw, landscape, "1920:1080", start, duration)
    record = {
        "source_url": source_url,
        "mock": False,
        "duration": duration,
        "start": start,
        "raw": raw.name,
        "vertical": vertical.name,
        "landscape": landscape.name,
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
    return {
        **record,
        "vertical_url": media_url(buyer_id, record["vertical"], base),
        "landscape_url": media_url(buyer_id, record["landscape"], base),
    }
