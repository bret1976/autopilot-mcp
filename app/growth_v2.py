"""Growth V2 for TrendPilot (ported from marketing-automation Growth V2).

* No untransformed reposts: a downloaded original only goes out after a hook,
  two commentary lines and a creator credit are burned in. Brand clips rendered
  from the buyer's own site count as original. Raw downloads are refused.
* YouTube title = AI hook (<= 60 chars), never an internal job/campaign name.
* Per-platform captions: short hook + 1-3 lines + 3-5 topic hashtags, no brand
  opener, no links on TikTok/YouTube/Instagram/Facebook; longer LinkedIn; short X.
* Topic steering toward short punchy "wow" clips for the buyer's brand, a 14-day
  per-source-creator cooldown and no repeat source URLs.
* Retry cap of 2 automation attempts per slot; PostProxy wallet/quota errors
  skip that platform for 24h with an alert; platform deletions raise alerts.

Kill switches (env):
  GROWTH_V2=0                    everything below off (legacy behavior)
  GROWTH_V2_TRANSFORM=0          never post downloaded clips at all (brand clips only)
  GROWTH_V2_MAX_SLOT_ATTEMPTS=N  automation attempts per slot (default 2)
  GROWTH_V2_MAX_SECONDS=N        transformed clip length cap (default 30)
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("growth_v2")

VERSION = "2026-10-14.1"
MAX_HOOK_CHARS = 60
CREATOR_COOLDOWN_DAYS = 14
REPEAT_WINDOW_DAYS = 365
PLATFORM_SKIP_HOURS = 24
SLOT_RETRY_SECONDS = 600
SHORT_PLATFORMS = ("tiktok", "youtube", "instagram", "facebook")
JUNK_TITLES = {"", "studio cut", "untitled cut", "viral clip", "public tiktok original", "autopilot", "automation"}

_URL_RE = re.compile(r"(https?://\S+|www\.\S+|\b[\w-]+\.(?:com|io|ai|co|net|org|app)(?:/\S*)?\b)", re.I)
_HASHTAG_RE = re.compile(r"#\w+")
_lock = threading.Lock()


# ---------------------------------------------------------------- switches
def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip().lower()


def enabled() -> bool:
    return _env("GROWTH_V2", "1") not in {"0", "false", "no", "off"}


def transform_enabled() -> bool:
    return _env("GROWTH_V2_TRANSFORM", "1") not in {"0", "false", "no", "off"}


def max_slot_attempts() -> int:
    try:
        return max(1, int(_env("GROWTH_V2_MAX_SLOT_ATTEMPTS", "2")))
    except ValueError:
        return 2


def max_seconds() -> float:
    try:
        return max(6.0, min(59.0, float(_env("GROWTH_V2_MAX_SECONDS", "30"))))
    except ValueError:
        return 30.0


# ---------------------------------------------------------------- state
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _state_path(buyer_id: str) -> Path:
    from app.config import data_dir

    root = data_dir() / "growth_v2"
    root.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", buyer_id or "anon")[:80]
    return root / f"{safe}.json"


def load_state(buyer_id: str) -> dict[str, Any]:
    path = _state_path(buyer_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    for key, default in (("history", []), ("alerts", []), ("skip_until", {}), ("slots", {}), ("posted", [])):
        data.setdefault(key, default)
    return data


def save_state(buyer_id: str, state: dict[str, Any]) -> None:
    state["history"] = state.get("history", [])[-500:]
    state["alerts"] = state.get("alerts", [])[-50:]
    state["posted"] = state.get("posted", [])[-200:]
    slots = state.get("slots", {})
    state["slots"] = dict(sorted(slots.items())[-60:])
    path = _state_path(buyer_id)
    tmp = path.with_suffix(".tmp")
    with _lock:
        tmp.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)


def add_alert(buyer_id: str, kind: str, message: str, state: dict[str, Any] | None = None) -> dict[str, Any]:
    own = state is None
    state = load_state(buyer_id) if own else state
    alert = {"at": _utcnow().isoformat(), "kind": kind, "message": message[:500]}
    state["alerts"].append(alert)
    logger.warning("GROWTH_V2 alert buyer=%s kind=%s %s", buyer_id, kind, message[:300])
    if own:
        save_state(buyer_id, state)
    return alert


def recent_alerts(buyer_id: str, limit: int = 10) -> list[dict[str, Any]]:
    return load_state(buyer_id).get("alerts", [])[-limit:]


# ---------------------------------------------------------------- text
def strip_links(text: str) -> str:
    out = _URL_RE.sub("", text or "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    return "\n".join(line.rstrip() for line in out.splitlines()).strip()


def strip_brand_opener(text: str, brand: str) -> str:
    text = (text or "").strip()
    if not brand:
        return text
    b = re.escape(brand.strip())
    pattern = re.compile(rf"^\s*(?:(?:at|here at|from|with|we at)\s+)?{b}\b[\s,:;!.\-\u2013\u2014|']*(?:s\b)?\s*", re.I)
    out = pattern.sub("", text, count=1).strip()
    if out and out != text:
        out = out[0].upper() + out[1:]
    return out or text


def _clean_line(text: Any, brand: str = "") -> str:
    text = strip_links(str(text or ""))
    text = _HASHTAG_RE.sub("", text)
    text = " ".join(text.split()).strip(" -\u2013\u2014|")
    # A line that only pointed at a link ("Visit dcrbn.com") is dropped, not left dangling.
    text = re.sub(r"(?:\b(?:visit|see|go to|check out|learn more|more|link|at|on|via|here)\b[\s:]*)+[.!]?$", "", text, flags=re.I).strip(" -:\u2013\u2014|")
    return strip_brand_opener(text, brand)


def clamp_chars(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-\u2013\u2014|")


def clean_hook(text: Any, brand: str = "") -> str:
    hook = _clean_line(text, brand).strip("\"'\u201c\u201d")
    hook = re.sub(r"#shorts", "", hook, flags=re.I).strip()
    words = hook.split()
    if len(words) > 12:
        hook = " ".join(words[:12])
    return clamp_chars(hook, MAX_HOOK_CHARS)


def normalize_tags(tags: Any, locked: list[str] | tuple[str, ...] | None = None, brand: str = "") -> list[str]:
    if isinstance(tags, str):
        tags = re.split(r"[\s,]+", tags)
    out: list[str] = []
    seen: set[str] = set()
    brand_key = re.sub(r"[^a-z0-9]", "", (brand or "").lower())
    for tag in list(tags or []):
        word = re.sub(r"[^\w]", "", str(tag).lstrip("#"))
        if not word or word.isdigit() or word.lower() in seen:
            continue
        if brand_key and word.lower() == brand_key:
            continue  # topic tags, not the brand name
        seen.add(word.lower())
        out.append(f"#{word}")
    # One brand tag the buyer locked in is allowed, at the end, within the 5 cap.
    for tag in list(locked or [])[:1]:
        word = re.sub(r"[^\w]", "", str(tag).lstrip("#"))
        if word and word.lower() not in seen and len(out) < 5:
            out = out[:4] + [f"#{word}"]
            seen.add(word.lower())
    for pad in ("#Shorts", "#Viral", "#Wow"):
        if len(out) >= 3:
            break
        if pad.lower().lstrip("#") not in seen:
            out.append(pad)
            seen.add(pad.lower().lstrip("#"))
    return out[:5]


def youtube_title_for(copy: dict[str, Any], brand: str = "") -> str:
    """AI hook <= 60 chars. Never the internal job / campaign / scan title."""
    candidates = [copy.get("hook"), copy.get("on_screen_hook")]
    for line in str((copy.get("captions") or {}).get("youtube") or "").splitlines():
        if _clean_line(line, brand):
            candidates.append(line)
            break
    candidates.append(copy.get("title"))
    for cand in candidates:
        title = clean_hook(cand or "", brand)
        if title and title.lower() not in JUNK_TITLES and not title.lower().startswith(("autopilot", "automation", "job ")):
            return title
    return clamp_chars(brand or "Watch this", MAX_HOOK_CHARS)


def build_captions(
    concept: dict[str, Any],
    platforms: list[str],
    *,
    brand: str = "",
    website: str = "",
    credit: str = "",
    source_url: str = "",
    locked: list[str] | tuple[str, ...] | None = None,
) -> dict[str, str]:
    hook = clean_hook(concept.get("hook") or "", brand)
    lines = [clamp_chars(_clean_line(l, brand), 110) for l in (concept.get("lines") or []) if _clean_line(l, brand)][:2]
    tags = normalize_tags(concept.get("hashtags") or concept.get("topic_tags"), locked, brand)
    tag_line = " ".join(tags)
    credit_line = f"Original clip: @{credit.lstrip('@')}" if credit else ""
    short_body = "\n".join([hook] + lines + ([credit_line] if credit_line else []))
    short = strip_links(f"{short_body}\n\n{tag_line}")
    angle = _clean_line(concept.get("linkedin_angle") or "", brand)
    li_parts = [hook] + lines + ([angle] if angle else [])
    if credit_line:
        li_parts.append(credit_line + (f" (source: {source_url})" if source_url else ""))
    if website:
        li_parts.append(website)
    linkedin = "\n\n".join(p for p in li_parts if p) + f"\n\n{tag_line}"
    x_tags = " ".join(tags[:2])
    tweet = clamp_chars(f"{hook} {lines[0]}" if lines else hook, 280 - len(x_tags) - 2)
    twitter = strip_links(f"{tweet}\n\n{x_tags}".strip())
    gbp = clamp_chars(" ".join([hook] + lines), 1400)
    out: dict[str, str] = {}
    for name in platforms:
        if name in SHORT_PLATFORMS:
            out[name] = short
        elif name == "linkedin":
            out[name] = linkedin
        elif name == "twitter":
            out[name] = twitter
        elif name == "google_business":
            out[name] = gbp
        else:
            out[name] = short
    return out


# ---------------------------------------------------------------- sources
def creator_from_url(url: str) -> str:
    url = url or ""
    for pattern in (
        r"tiktok\.com/@([\w.]+)",
        r"instagram\.com/(?!p/|reel/|reels/)([\w.]+)/",
        r"(?:x|twitter)\.com/(\w+)/status",
        r"reddit\.com/(?:user|u)/([\w-]+)",
        r"youtube\.com/@([\w.-]+)",
        r"vimeo\.com/([A-Za-z][\w-]+)",
    ):
        match = re.search(pattern, url, re.I)
        if match:
            return match.group(1).lower()
    return ""


def _norm_url(url: str) -> str:
    url = (url or "").strip()
    try:
        p = urlparse(url)
    except ValueError:
        return url.lower()
    host = (p.netloc or "").lower().removeprefix("www.").removeprefix("m.")
    if host in {"twitter.com", "mobile.twitter.com"}:
        host = "x.com"
    return f"{host}{p.path.rstrip('/')}".lower() + (f"?{p.query}" if "youtube.com" in host and p.query else "")


def blocked_sources(buyer_id: str, now: datetime | None = None) -> tuple[set[str], set[str]]:
    """(used source URLs within REPEAT_WINDOW_DAYS, creators within CREATOR_COOLDOWN_DAYS)."""
    now = now or _utcnow()
    state = load_state(buyer_id)
    urls: set[str] = set()
    creators: set[str] = set()
    for row in state.get("history", []):
        at = _parse(row.get("at")) or now
        if row.get("source_url") and at >= now - timedelta(days=REPEAT_WINDOW_DAYS):
            urls.add(_norm_url(row["source_url"]))
        if row.get("creator") and at >= now - timedelta(days=CREATOR_COOLDOWN_DAYS):
            creators.add(str(row["creator"]).lower())
    return urls, creators


def filter_candidates(buyer_id: str, candidates: list[dict[str, Any]], now: datetime | None = None) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not enabled():
        return candidates, []
    urls, creators = blocked_sources(buyer_id, now)
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for item in candidates:
        url = str(item.get("source_url") or "")
        creator = creator_from_url(url)
        if _norm_url(url) in urls:
            dropped.append({"url": url, "code": "repeat_source", "error": "Already posted this source; repeats are blocked."})
        elif creator and creator in creators:
            dropped.append({"url": url, "code": "creator_cooldown", "error": f"@{creator} was used in the last {CREATOR_COOLDOWN_DAYS} days."})
        else:
            kept.append(item)
    return kept, dropped


def steering_text(buyer_id: str, brand: str) -> str:
    if not enabled():
        return ""
    _, creators = blocked_sources(buyer_id)
    avoid = ", ".join(f"@{c}" for c in sorted(creators)[:30]) or "(none)"
    return (
        f"\nPick short, punchy 'wow' clips (ideally 6-30 seconds) with a clear visual payoff in the first 2 seconds "
        f"that are directly relevant to what {brand} does and its audience. Skip talking heads, slideshows, "
        f"memes and long explainers. Each candidate must come from a different creator. "
        f"Do NOT return clips from these creators (used in the last {CREATOR_COOLDOWN_DAYS} days): {avoid}."
    )


def record_post(buyer_id: str, *, source_url: str, topic: str = "", hook: str = "", post_ids: list[str] | None = None) -> None:
    state = load_state(buyer_id)
    state["history"].append({
        "at": _utcnow().isoformat(),
        "source_url": source_url,
        "creator": creator_from_url(source_url),
        "topic": topic[:120],
        "hook": hook[:120],
    })
    for pid in post_ids or []:
        state["posted"].append({"id": pid, "at": _utcnow().isoformat(), "checked_at": None})
    save_state(buyer_id, state)


# ---------------------------------------------------------------- copy
COPY_PROMPT = """You write short-form social copy for {brand} ({website}) about a clip it is reacting to.
Voice: {voice}
Clip title: {title}
Why it is moving: {why}
Notes: {notes}
Topic ideas: {tags}

Rules: the hook is a curiosity hook of about 10 words (max 60 characters), no emoji, no hashtags,
never starts with the brand name or "At {brand}". No links anywhere. Never use: game-changer,
revolutionize, unlock, next-level, crush, viral hack.

Return JSON only:
{{
  "hook": "about 10 words, <= 60 chars",
  "on_screen_hook": "max 6 words for the video overlay",
  "commentary": ["short factual line under 60 chars adding context the clip lacks", "second line under 60 chars"],
  "lines": ["1-2 short caption lines under 100 chars"],
  "hashtags": ["3-5 topic hashtags, no brand tags"],
  "linkedin_angle": "one sentence on why this matters to {brand}'s audience",
  "topic": "short topic"
}}
"""


def sanitize_concept(raw: Any, brand: str = "") -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    hook = clean_hook(raw.get("hook") or "", brand)
    return {
        "hook": hook,
        "on_screen_hook": clamp_chars(_clean_line(raw.get("on_screen_hook") or hook, brand), 40),
        "commentary": [clamp_chars(_clean_line(c, brand), 64) for c in (raw.get("commentary") or []) if _clean_line(c, brand)][:2],
        "lines": [clamp_chars(_clean_line(l, brand), 110) for l in (raw.get("lines") or []) if _clean_line(l, brand)][:2],
        "hashtags": raw.get("hashtags") or [],
        "linkedin_angle": clamp_chars(_clean_line(raw.get("linkedin_angle") or "", brand), 220),
        "topic": clamp_chars(_clean_line(raw.get("topic") or hook, brand), 80),
    }


# ---------------------------------------------------------------- video transform
def _font(size: int):
    from PIL import ImageFont

    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def _wrap(draw, text: str, font, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = f"{cur} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines[:4]


def _text_png(path: Path, size: tuple[int, int], text: str, *, font_size: int, y_frac: float, alpha: int = 160) -> Path:
    from PIL import Image, ImageDraw

    w, h = size
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _font(font_size)
    lines = _wrap(draw, text, font, int(w * 0.84))
    line_h = int(font_size * 1.25)
    block_h = line_h * len(lines)
    top = int(h * y_frac)
    pad = int(font_size * 0.4)
    widest = max((draw.textlength(l, font=font) for l in lines), default=0)
    draw.rectangle([(w - widest) / 2 - pad, top - pad, (w + widest) / 2 + pad, top + block_h + pad], fill=(0, 0, 0, alpha))
    for i, line in enumerate(lines):
        lw = draw.textlength(line, font=font)
        draw.text(((w - lw) / 2, top + i * line_h), line, font=font, fill=(255, 255, 255, 255))
    img.save(path)
    return path


def _probe_duration(path: Path) -> float:
    import shutil
    import subprocess

    probe = shutil.which("ffprobe")
    if not probe:
        return 0.0
    res = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=60)
    try:
        return float((res.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def render_transformed(src: Path, dst: Path, size: tuple[int, int], *, hook: str, commentary: list[str], footer: str, seconds: float) -> None:
    """Burn the hook (0-3s), two timed commentary lines and a persistent credit footer into src."""
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed on this host")
    work = dst.parent
    stem = dst.stem
    vertical = size[1] > size[0]
    pngs = [
        _text_png(work / f"{stem}-o0.png", size, hook.upper(), font_size=78 if vertical else 64, y_frac=0.13 if vertical else 0.08),
        _text_png(work / f"{stem}-o1.png", size, commentary[0], font_size=54 if vertical else 48, y_frac=0.68 if vertical else 0.72),
        _text_png(work / f"{stem}-o2.png", size, commentary[1], font_size=54 if vertical else 48, y_frac=0.68 if vertical else 0.72),
        _text_png(work / f"{stem}-o3.png", size, footer, font_size=32 if vertical else 30, y_frac=0.90 if vertical else 0.92, alpha=120),
    ]
    mid = max(4.0, min(seconds * 0.55, seconds - 2))
    w, h = size
    filt = (
        f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1[base];"
        f"[base][1:v]overlay=0:0:enable='lt(t,3)'[a];"
        f"[a][2:v]overlay=0:0:enable='between(t,3,{mid:.2f})'[b];"
        f"[b][3:v]overlay=0:0:enable='gte(t,{mid:.2f})'[c];"
        f"[c][4:v]overlay=0:0,format=yuv420p[v]"
    )
    cmd = [ffmpeg, "-y", "-i", str(src)]
    for p in pngs:
        cmd += ["-loop", "1", "-i", str(p)]
    cmd += ["-t", f"{seconds:.2f}", "-filter_complex", filt, "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "128k",
            "-shortest", "-movflags", "+faststart", str(dst)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.returncode != 0 or not dst.exists():
            raise RuntimeError(f"ffmpeg transform failed: {(res.stderr or '')[-400:]}")
    finally:
        for p in pngs:
            try:
                p.unlink()
            except OSError:
                pass


def _local_media(buyer_id: str, url_or_name: str) -> Path | None:
    from app.media import buyer_media_dir

    if not url_or_name:
        return None
    path = urlparse(url_or_name).path if "://" in url_or_name or url_or_name.startswith("/") else url_or_name
    name = path.rstrip("/").rsplit("/", 1)[-1]
    if not name or "/" in name or name.startswith("."):
        return None
    if "://" in url_or_name and f"/media/{buyer_id}/" not in path:
        return None
    candidate = buyer_media_dir(buyer_id) / name
    return candidate if candidate.exists() else None


def classify_media(buyer_id: str, media: dict[str, Any]) -> str:
    """'transformed' | 'original' | 'downloaded' | 'external'."""
    if media.get("transformed"):
        return "transformed"
    src = str(media.get("media_source") or "")
    if src == "brand_fallback":
        return "original"
    if src == "growth_v2_transformed":
        return "transformed"
    local = _local_media(buyer_id, str(media.get("vertical") or media.get("vertical_url") or media.get("landscape_url") or ""))
    if local is None and not media.get("raw"):
        return "external"
    if local is not None:
        if "-g2-" in local.name:
            return "transformed"
        stem = local.name.split("-")[0]
        if local.name.startswith("brand-"):
            return "original"
        meta = local.parent / f"{stem}.json"
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            if info.get("media_source") == "brand_fallback":
                return "original"
        except (OSError, ValueError):
            pass
    return "downloaded"


def transform_media(buyer_id: str, media: dict[str, Any], copy: dict[str, Any]) -> dict[str, Any]:
    """Return a new media dict whose clips carry burned-in hook + commentary + credit."""
    from app.media import buyer_media_dir, media_url

    if not transform_enabled():
        raise RuntimeError("GROWTH_V2_TRANSFORM=0: downloaded clips are switched off; refusing to post one.")
    hook = clean_hook(copy.get("on_screen_hook") or copy.get("hook") or "")
    commentary = [c for c in (copy.get("commentary") or []) if c][:2]
    if not hook or len(commentary) < 2:
        raise RuntimeError("No usable hook/commentary for this clip; refusing to post a downloaded clip as-is.")
    source_url = str(media.get("source_url") or copy.get("source_url") or "")
    creator = creator_from_url(source_url)
    brand = str(copy.get("brand") or "")
    footer = (f"Original: @{creator}" if creator else (f"Original: {urlparse(source_url).netloc.removeprefix('www.')}" if source_url else "Original clip")) + (f"  |  Commentary: {brand}" if brand else "")
    dest = buyer_media_dir(buyer_id)
    out = dict(media)
    seconds = max_seconds()
    tag = hashlib.sha256(f"{source_url}|{hook}|{'|'.join(commentary)}".encode()).hexdigest()[:10]
    base = None
    try:
        from app.store import load_buyer

        base = (load_buyer(buyer_id) or {}).get("public_base_url") or None
    except Exception:  # noqa: BLE001
        base = None
    for key, size in (("vertical", (1080, 1920)), ("landscape", (1920, 1080))):
        src = _local_media(buyer_id, str(media.get(key) or media.get(f"{key}_url") or ""))
        if src is None:
            raise RuntimeError(f"Cannot find the local {key} master to transform; refusing to post it untransformed.")
        dur = _probe_duration(src) or seconds
        target = dest / f"{tag}-g2-{'9x16' if key == 'vertical' else '16x9'}.mp4"
        render_transformed(src, target, size, hook=hook, commentary=commentary, footer=footer, seconds=min(seconds, dur))
        out[key] = target.name
        out[f"{key}_url"] = media_url(buyer_id, target.name, base)
    out.update({
        "transformed": True,
        "media_source": "growth_v2_transformed",
        "transform": {"hook": hook, "commentary": commentary, "credit": footer, "max_seconds": seconds},
        "source_creator": creator,
    })
    return out


def ensure_publishable(buyer_id: str, media: dict[str, Any], copy: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """(media to post, '') or (None, reason). Raw downloads never go out."""
    if not enabled():
        return media, ""
    kind = classify_media(buyer_id, media)
    if kind in {"transformed", "original", "external"}:
        return media, ""
    try:
        return transform_media(buyer_id, media, copy), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"Refused: downloaded clip is not transformed ({str(exc)[:300]})."


# ---------------------------------------------------------------- PostProxy errors
_QUOTA_WORDS = ("wallet", "quota", "top up", "top-up", "insufficient", "credits", "payment required", "limit reached", "402")


def is_quota_error(text: str) -> bool:
    low = (text or "").lower()
    return any(w in low for w in _QUOTA_WORDS)


def platform_skipped(buyer_id: str, platform: str, now: datetime | None = None) -> str:
    if not enabled():
        return ""
    until = _parse(load_state(buyer_id).get("skip_until", {}).get(platform))
    if until and until > (now or _utcnow()):
        return until.isoformat()
    return ""


def note_publish_error(buyer_id: str, platform: str, error: str) -> bool:
    """Wallet/quota: skip this platform for 24h + alert. Returns True when skipped."""
    if not enabled() or not is_quota_error(error):
        return False
    state = load_state(buyer_id)
    until = _utcnow() + timedelta(hours=PLATFORM_SKIP_HOURS)
    state["skip_until"][platform] = until.isoformat()
    add_alert(buyer_id, "postproxy_quota", f"PostProxy wallet/quota error on {platform}; skipping {platform} for {PLATFORM_SKIP_HOURS}h. {error[:200]}", state)
    save_state(buyer_id, state)
    return True


_DELETED_WORDS = ("deleted", "removed", "taken down", "not_found", "rejected")


def deletion_notes(payload: Any) -> list[str]:
    from app import postproxy

    notes = []
    for row in postproxy.platform_outcomes(payload):
        status = " ".join(str(row.get(k) or "") for k in ("status", "state", "error", "error_message")).lower()
        if any(w in status for w in _DELETED_WORDS):
            notes.append(f"{row.get('platform') or row.get('network') or 'platform'}: {status.strip()[:200]}")
    return notes


async def check_deletions(record: dict[str, Any], max_checks: int = 10) -> list[dict[str, Any]]:
    """Poll recent PostProxy posts; alert when a platform reports the post deleted/removed."""
    from app import postproxy

    buyer_id = str(record.get("buyer_id") or "")
    key = record.get("postproxy_api_key") or ""
    if not enabled() or not buyer_id or not key:
        return []
    state = load_state(buyer_id)
    now = _utcnow()
    alerts = []
    checked = 0
    for row in state.get("posted", []):
        at = _parse(row.get("at")) or now
        last = _parse(row.get("checked_at"))
        if row.get("deleted") or at < now - timedelta(days=7) or (last and last > now - timedelta(hours=6)):
            continue
        if checked >= max_checks:
            break
        checked += 1
        row["checked_at"] = now.isoformat()
        try:
            payload = await postproxy.get_post(key, str(row["id"]))
        except Exception as exc:  # noqa: BLE001
            if "404" in str(exc):
                row["deleted"] = True
                alerts.append(add_alert(buyer_id, "platform_deleted", f"PostProxy post {row['id']} is gone (404).", state))
            continue
        notes = deletion_notes(payload)
        if notes:
            row["deleted"] = True
            alerts.append(add_alert(buyer_id, "platform_deleted", f"Post {row['id']} was deleted/removed: {'; '.join(notes)}", state))
    save_state(buyer_id, state)
    return alerts


# ---------------------------------------------------------------- slot retry cap
def slot_attempts(buyer_id: str, slot: str) -> dict[str, Any]:
    return dict(load_state(buyer_id).get("slots", {}).get(slot) or {})


def note_slot_attempt(buyer_id: str, slot: str, *, ok: bool | None = None) -> dict[str, Any]:
    state = load_state(buyer_id)
    row = state["slots"].setdefault(slot, {"attempts": 0, "ok": False})
    if ok is None:
        row["attempts"] = int(row.get("attempts") or 0) + 1
        row["last_at"] = _utcnow().isoformat()
    else:
        row["ok"] = bool(ok)
        row["finished_at"] = _utcnow().isoformat()
    save_state(buyer_id, state)
    return row


def slot_retry_due(buyer_id: str, slot: str, now: datetime | None = None) -> bool:
    """A failed slot may retry once (cap = GROWTH_V2_MAX_SLOT_ATTEMPTS) after 10 minutes."""
    if not enabled():
        return False
    row = slot_attempts(buyer_id, slot)
    if not row or row.get("ok") or not row.get("finished_at"):
        return False
    if int(row.get("attempts") or 0) >= max_slot_attempts():
        return False
    last = _parse(row.get("last_at"))
    return bool(last and (now or _utcnow()) - last >= timedelta(seconds=SLOT_RETRY_SECONDS))


def health() -> dict[str, Any]:
    return {
        "enabled": enabled(),
        "version": VERSION,
        "transform": transform_enabled(),
        "max_slot_attempts": max_slot_attempts(),
        "max_seconds": max_seconds(),
        "creator_cooldown_days": CREATOR_COOLDOWN_DAYS,
    }
