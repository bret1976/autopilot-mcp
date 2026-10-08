"""Residential pull proxy for YouTube originals.

Railway's datacenter IP gets YouTube's "Sign in to confirm you're not a bot" on
every yt-dlp client. Bret's Mac Mini (residential IP) runs a tiny authenticated
pull service behind a Cloudflare tunnel. autopilot-mcp asks it for the trimmed
window of an original and transcodes the returned mp4 here as usual.

Config:
  YT_DOWNLOAD_PROXY_TOKEN  shared secret (X-Proxy-Token), required
  YT_DOWNLOAD_PROXY_URL    tunnel base URL (fallback)
  DATA_DIR/yt_proxy.json   URL registered by the Mini watchdog (preferred; quick
                           tunnel URLs change on restart, so the Mini re-registers
                           without a Railway redeploy)
"""
from __future__ import annotations

import hmac
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import data_dir

PULL_TIMEOUT_SECONDS = 330.0
_FILE = "yt_proxy.json"


def token() -> str:
    return (os.environ.get("YT_DOWNLOAD_PROXY_TOKEN") or "").strip()


def _state_path() -> Path:
    return data_dir() / _FILE


def registered() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def urls() -> list[str]:
    """Candidate base URLs, registered (fresh) first, then the env var."""
    out: list[str] = []
    for value in (registered().get("url"), os.environ.get("YT_DOWNLOAD_PROXY_URL")):
        value = str(value or "").strip().rstrip("/")
        if value.startswith("https://") and value not in out:
            out.append(value)
    return out


def configured() -> bool:
    return bool(token() and urls())


def token_ok(candidate: str | None) -> bool:
    expected = token()
    return bool(expected and candidate) and hmac.compare_digest(str(candidate).strip(), expected)


def register(url: str) -> dict[str, Any]:
    url = (url or "").strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError("url must be an https base URL")
    record = {"url": url, "registered_at": int(time.time())}
    path = _state_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record), encoding="utf-8")
    tmp.replace(path)
    return record


def summary() -> dict[str, Any]:
    reg = registered()
    return {
        "configured": configured(),
        "registered_at": reg.get("registered_at"),
        "env_url_set": bool((os.environ.get("YT_DOWNLOAD_PROXY_URL") or "").strip()),
    }


class ProxyError(RuntimeError):
    pass


def pull(source_url: str, dest: Path, *, start: float, duration: float) -> bool:
    """Download the trimmed window to dest through the Mini. Returns True if the
    returned file is already sectioned (starts at `start`), False if it is the full
    video. Raises ProxyError when no proxy URL works."""
    import httpx

    secret = token()
    if not secret:
        raise ProxyError("YT_DOWNLOAD_PROXY_TOKEN is not set")
    errors: list[str] = []
    for base in urls():
        try:
            with httpx.Client(timeout=httpx.Timeout(PULL_TIMEOUT_SECONDS, connect=15.0)) as client:
                with client.stream(
                    "POST",
                    f"{base}/pull",
                    headers={"x-proxy-token": secret, "content-type": "application/json"},
                    json={"url": source_url, "start": start, "duration": duration},
                ) as response:
                    if response.status_code != 200:
                        body = response.read()[:400].decode("utf-8", "ignore")
                        errors.append(f"{urlparse(base).netloc}: HTTP {response.status_code} {body}")
                        continue
                    with dest.open("wb") as fh:
                        for chunk in response.iter_bytes():
                            fh.write(chunk)
                    sectioned = response.headers.get("x-ytpp-sectioned", "1") == "1"
            if dest.exists() and dest.stat().st_size > 1000:
                return sectioned
            errors.append(f"{urlparse(base).netloc}: empty file")
        except Exception as exc:  # noqa: BLE001 — try the next URL, then local yt-dlp
            errors.append(f"{urlparse(base).netloc}: {type(exc).__name__}: {str(exc)[:200]}")
    raise ProxyError("; ".join(errors) or "no YT download proxy URL configured")
