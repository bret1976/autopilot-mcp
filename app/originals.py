"""Growth V2 originals for TrendPilot (ported from marketing-automation PRs #3-#8, 2026-10-14).

Every automated slot now posts a NEW AI-generated original instead of reposting someone else's clip:

* plan a concept with a 3-beat shot list (Gemini), on a topic not used in the last 14 days
  (loose word matching), with a number in the on-screen hook and a comment question;
* render it with Gemini Omni 1.1 Flash (720p) as a native 9:16, 8-second clip (Veo 3.1 Preview was shut down 2026-10-22),
  and every failed attempt gets a brand-new concept on a new topic;
* sample frames and ask Gemini (gemini-3.6-flash) whether the video shows what the caption says.
  On a mismatch, rewrite the caption from the frames and re-check; if it still fails, regenerate.
  A mismatch is never published;
* mix a short synthesized whoosh into a quiet first second, burn the hook in for 2.8s, and end with
  a 0.2s audio fade at the end.

The old repost path (download + transform someone else's clip) only runs when
GROWTH_V2_REPOST_FALLBACK=1 is set. It is OFF by default.

Env:
  GROWTH_V2_ORIGINALS=0              turn originals off (legacy scan/transform path)
  GROWTH_V2_REPOST_FALLBACK=1        allow the repost/transform path after all originals fail
  GROWTH_VIDEO_ENGINES=a,b,...       override the Omni retry ladder (a second clip only fits the cap after an unbilled failure)
  GROWTH_RETRY_BACKOFF=0,15,30,...   seconds to wait before each attempt
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

from app import gemini_budget, growth_v2
from app.gemini import parse_json_object

logger = logging.getLogger("growth_v2.originals")

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
VISION_MODEL = "gemini-3.6-flash"
CONCEPT_MODELS = ("gemini-3.6-flash",)
# 2026-10-25 (Bret): Veo 3.1 Preview (lite/fast) was shut down by Google 2026-10-22, so every attempt runs on
# Gemini Omni 1.1 Flash (720p, native 9:16, 8 s), ported from bret1976/marketing-automation (PR #17).
OMNI_ENGINE = "gemini_omni"
OMNI_MODEL = "gemini-omni-1.1-flash"
OMNI_RESOLUTION = "720p"
OMNI_VIDEO_TOKENS_PER_SECOND = 5792
OMNI_VIDEO_USD_PER_MTOKEN = 17.50
OMNI_TEXT_OUT_USD_PER_MTOKEN = 9.00
OMNI_INPUT_USD_PER_MTOKEN = 1.50
# Hard cap on billed output tokens per clip: 46,800 x $17.50/1M = $0.819 (+ <$0.001 prompt) worst case.
OMNI_MAX_OUTPUT_TOKENS = 46800
OMNI_MAX_CLIP_USD = 0.82
OMNI_TIMEOUT_SECONDS = 600
VEO_MODELS = {OMNI_ENGINE: OMNI_MODEL}  # engine -> model (name kept for callers)
# Retries stay on Omni; under the $0.85 cap a second clip only fits after an unbilled failure.
DEFAULT_ENGINE_LADDER = (OMNI_ENGINE, OMNI_ENGINE, OMNI_ENGINE)
DEFAULT_BACKOFF_SECONDS = (0, 15, 30, 60, 90)
CLIP_SECONDS = 8
# Cost guard: hard cap per Short, checked before every paid video call (worst case reserved).
PLAN_USD = 0.0125   # concept planning text calls (estimate)
CHECK_USD = 0.0125  # frame check vision calls (estimate)
DEFAULT_COST_CAP_USD = 0.85
MAX_COST_CAP_USD = 0.85


def cost_cap() -> float:
    """SHORTS_COST_CAP_USD env override; default $0.85, never above $0.85."""
    try:
        return min(MAX_COST_CAP_USD, max(0.1, float(os.environ.get("SHORTS_COST_CAP_USD") or DEFAULT_COST_CAP_USD)))
    except ValueError:
        return DEFAULT_COST_CAP_USD


def clip_cost(engine: str = OMNI_ENGINE) -> float:
    """Worst case one 8 s Omni clip can bill (reserved before the call)."""
    return OMNI_MAX_CLIP_USD


def omni_expected_cost(seconds: float = CLIP_SECONDS) -> float:
    return round(seconds * OMNI_VIDEO_TOKENS_PER_SECOND * OMNI_VIDEO_USD_PER_MTOKEN / 1_000_000, 4)


def omni_cost_from_usage(usage: Any) -> float | None:
    """Actual cost from Interactions `usage` (None if Google did not return usage)."""
    if not isinstance(usage, dict) or not usage:
        return None
    video_out = text_out = 0
    for item in usage.get("output_tokens_by_modality") or []:
        if str(item.get("modality") or "").lower() == "video":
            video_out += int(item.get("tokens") or 0)
        else:
            text_out += int(item.get("tokens") or 0)
    total_out = int(usage.get("total_output_tokens") or 0)
    if not video_out and total_out:
        video_out = total_out
    elif total_out > video_out + text_out:
        text_out = total_out - video_out
    text_out += int(usage.get("total_thought_tokens") or 0)
    inp = int(usage.get("total_input_tokens") or 0)
    return round(video_out * OMNI_VIDEO_USD_PER_MTOKEN / 1e6 + text_out * OMNI_TEXT_OUT_USD_PER_MTOKEN / 1e6
                 + inp * OMNI_INPUT_USD_PER_MTOKEN / 1e6, 4)


# Style: "cinematic recreation" (modeled on 6Frame Studio's best-engaging post, Omaha Beach 1944):
# a widescreen 16:9 clip shown blur-padded inside the 9:16 frame, desaturated epic grade, 2-3 hard cuts,
# orchestral score + SFX. Override the generation aspect with ORIGINALS_STYLE_ASPECT=9:16.
STYLE_NAME = "cinematic-recreation-v1"
STYLE_ASPECT = "16:9"
STYLE_HASHTAGS = ["#CinematicAI", "#AIFilmmaking", "#AICinema"]


def style_aspect() -> str:
    v = (os.environ.get("ORIGINALS_STYLE_ASPECT") or STYLE_ASPECT).strip()
    return v if v in ("16:9", "9:16") else STYLE_ASPECT


def omni_prompt(prompt: str, seconds: int = CLIP_SECONDS, aspect_ratio: str = "9:16") -> str:
    prompt = (prompt or "").strip()
    if f"exactly {seconds} seconds" in prompt.lower():
        return prompt
    frame = "widescreen 16:9" if aspect_ratio == "16:9" else "vertical 9:16"
    return (f"A {seconds}-second {frame} cinematic clip, exactly {seconds} seconds long "
            f"([0-{seconds}s]), with natural synchronized sound. {prompt}").strip()


def omni_request_body(prompt: str, seconds: int = CLIP_SECONDS, aspect_ratio: str = "9:16") -> dict[str, Any]:
    return {
        "model": OMNI_MODEL,
        "input": omni_prompt(prompt, seconds, aspect_ratio),
        "response_format": {"type": "video", "aspect_ratio": aspect_ratio, "resolution": OMNI_RESOLUTION},
        "generation_config": {"max_output_tokens": OMNI_MAX_OUTPUT_TOKENS, "thinking_level": "low"},
        "background": False, "store": False, "stream": False,
    }


def _omni_video_part(body: dict[str, Any]) -> dict[str, Any] | None:
    for step in body.get("steps") or body.get("outputs") or []:
        contents = step.get("content") if isinstance(step, dict) else None
        if isinstance(contents, dict):
            contents = [contents]
        for part in contents or []:
            if isinstance(part, dict) and (part.get("type") == "video" or str(part.get("mime_type", "")).startswith("video/")):
                return part
    ov = body.get("output_video")
    return ov if isinstance(ov, dict) else None


def veo_failure_billed(exc: Any) -> bool:
    low = str(exc).lower()
    return any(t in low for t in ("timed out", "timeout", "download"))
VEO_POLL_SECONDS = 10
VEO_TIMEOUT_SECONDS = 600
TOPIC_DEDUPE_DAYS = 14
AUDIO_FADE_SECONDS = 0.2
OPENING_SOUND_MIN_DB = -38.0
HOOK_SECONDS = 2.8


class OriginalFailed(RuntimeError):
    pass


# ---------------------------------------------------------------- switches
def originals_enabled() -> bool:
    return growth_v2.enabled() and growth_v2._env("GROWTH_V2_ORIGINALS", "1") not in {"0", "false", "no", "off"}


def repost_fallback_enabled() -> bool:
    """The repost-of-others'-clips backup. OFF unless GROWTH_V2_REPOST_FALLBACK=1."""
    return growth_v2._env("GROWTH_V2_REPOST_FALLBACK", "0") in {"1", "true", "yes", "on"}


def engine_ladder() -> list[str]:
    raw = os.environ.get("GROWTH_VIDEO_ENGINES")
    ladder = [e.strip() for e in raw.split(",") if e.strip() in VEO_MODELS] if raw else list(DEFAULT_ENGINE_LADDER)
    return ladder[:8] or list(DEFAULT_ENGINE_LADDER)


def backoff_seconds(i: int) -> float:
    raw = os.environ.get("GROWTH_RETRY_BACKOFF")
    try:
        seq = [float(x) for x in raw.split(",") if x.strip()] if raw else list(DEFAULT_BACKOFF_SECONDS)
    except ValueError:
        seq = list(DEFAULT_BACKOFF_SECONDS)
    return seq[min(i, len(seq) - 1)] if seq else 0.0



def _api_key(record: dict[str, Any]) -> str:
    """The account's OWN Gemini key only. Never falls back to the server's key (a tester must not bill Bret)."""
    return str(record.get("gemini_api_key") or "").strip()


# ---------------------------------------------------------------- topic dedupe (14 days, loose)
_TOPIC_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "at", "for", "with", "by", "from", "how", "why",
               "what", "this", "that", "its", "it", "is", "are", "into", "vs", "versus", "explosion", "explosive",
               "physics", "science", "engineering", "slow", "motion", "macro", "shot", "video", "ai", "test", "shatter",
               "shattering", "destroy", "destroys", "breaking", "break", "experiment", "real", "extreme", "wow"}


def topic_tokens(text: Any) -> set[str]:
    words = re.findall(r"[a-z0-9]+", str(text or "").lower().replace("'s ", " ").replace("\u2019s ", " "))
    out = set()
    for w in words:
        if w in _TOPIC_STOP or len(w) < 3:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def topics_similar(a: Any, b: Any) -> bool:
    ta, tb = topic_tokens(a), topic_tokens(b)
    if not ta or not tb:
        return False
    common = ta & tb
    return len(common) >= 2 or (len(common) >= 1 and len(common) / min(len(ta), len(tb)) >= 0.67)


def recent_topics(buyer_id: str, days: int = TOPIC_DEDUPE_DAYS) -> list[str]:
    from datetime import timedelta

    state = growth_v2.load_state(buyer_id)
    cutoff = growth_v2._utcnow() - timedelta(days=days)
    out: list[str] = []
    for row in state.get("history", []):
        at = growth_v2._parse(row.get("at"))
        if at and at >= cutoff:
            out += [row.get("topic") or "", row.get("topic_key") or "", row.get("hook") or ""]
    return [t for t in out if topic_tokens(t)]


def is_recent_topic(concept: dict[str, Any], recent: list[str]) -> str | None:
    for field in ("topic", "topic_key"):
        for r in recent:
            if topics_similar(concept.get(field), r):
                return r
    return None


# ---------------------------------------------------------------- concept
CONCEPT_PROMPT = """You are the short-form producer for {brand} ({website}). Audience and niche: {niche}.
Brand voice: {voice}
Invent ONE original 8-second CINEMATIC RECREATION: an epic, movie-trailer style AI recreation of a famous,
real, well-documented moment, place or feat that {brand}'s audience cares about (history, engineering,
exploration, industry, nature, science, the craft behind {niche}). It must be factually correct
(right place, year, vehicles, gear, landscape).
The whole clip is generated by an AI video model, so: no identifiable real people's faces (people only as
distant, helmeted, hooded or from-behind figures), no logos, no copyrighted characters, no gore,
no text inside the video.
LOOK: widescreen 16:9 framing, desaturated slate-blue / steel-gray color grade with warm accents only from
fire or light sources, overcast volumetric haze, authentic period newsreel feel (1/50 shutter, slight motion
blur, fine grain), dense particles (smoke, spray, dust, snow or sparks). The camera always moves FORWARD
(low tracking pass, aerial push-in, over-the-shoulder push).
PACING: 2-3 hard cuts to different angles of the same moment: 0-3s wide establishing shot of the scale,
3-5.5s closer human-scale angle, 5.5-8s the most intense close action.
FIRST SECOND: already in motion with a loud, distinct sound (blast, boom, roar, crack). Name it in veo_prompt.
SOUND: epic orchestral score (brass, strings, war drums) plus matching sound effects; no dialogue, no narration.

Return ONLY JSON with these keys:
  topic: short name
  topic_key: 3-6 lowercase words identifying the subject
  title_line: "<Place> <Year>" in Title Case, 2-5 words, e.g. "Omaha Beach 1944" (style only, never reuse);
        no "#", no series or brand name, no emoji. Must differ from every one of these earlier titles: {avoid_titles}
  shot_list: array of exactly 3 beats, each {{"t": "0-3s" | "3-5.5s" | "5.5-8s", "shows": concrete visible action}};
        the 0-3s beat names the opening sound. Each beat must be something a camera can literally see.
  veo_prompt: 60-120 words, photoreal, written as that same shot list in order with hard cuts, widescreen 16:9,
        naming the grade, the forward camera moves, the particles and the orchestral score
  on_screen_hook: max 6 words, ALL CAPS place and year with the year in digits, e.g. "D-DAY · 1944"
  comment_question: one short question (max 80 characters, ends with "?") inviting a comment about the moment,
        no "like and subscribe", no "comment below"
  hook: caption first line, max 60 characters, starting "<Place> <year>." then a short dramatic phrase;
        no hashtags, emoji or links, must NOT start with the brand name
  lines: exactly 2 lines under 100 characters: "We recreated ... using generative video." and
        "Technical focus: ..." (2-3 craft points such as newsreel shutter, particle spray, deep framing)
  RULE: hook, on_screen_hook and lines may only describe things that appear in shot_list.
  hashtags: 2-3 topic hashtags about the moment, no brand hashtags
  linkedin_angle: one plain sentence on the craft lesson for {brand}'s audience

Do NOT reuse any of these recent topics (or anything close): {avoid}
"""


def style_hashtags(tags: Any) -> list[str]:
    """Up to 2 topic tags plus the cinematic style tags (max 5 total)."""
    if isinstance(tags, str):
        tags = tags.split()
    out: list[str] = []
    for t in tags or []:
        t = "#" + re.sub(r"[^A-Za-z0-9_]", "", str(t))
        if len(t) > 2 and t.lower() not in {x.lower() for x in out + STYLE_HASHTAGS} and len(out) < 2:
            out.append(t)
    return (out or ["#History"]) + STYLE_HASHTAGS


TITLE_REPEAT_DAYS = 365
_TITLE_JUNK = re.compile(r"(?:\b(?:episode|ep\.?|part|series)\s*\d*\b|#\s*\d+|\bno\.\s*\d+)", re.I)


def clean_title(value: Any, brand: str = "") -> str:
    """Per-post top title: 2-6 words, no numbering/series markers."""
    t = growth_v2._clean_line(value or "", brand).strip("\"'\u201c\u201d")
    t = _TITLE_JUNK.sub("", t)
    t = " ".join(t.split()).strip(" -:\u2013\u2014|.")
    words = t.split()
    if len(words) > 6:
        t = " ".join(words[:6])
    return growth_v2.clamp_chars(t, 48)


def _title_key(text: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def recent_titles(buyer_id: str, days: int = TITLE_REPEAT_DAYS) -> list[str]:
    from datetime import timedelta

    state = growth_v2.load_state(buyer_id)
    cutoff = growth_v2._utcnow() - timedelta(days=days)
    out = []
    for row in state.get("history", []):
        at = growth_v2._parse(row.get("at"))
        if row.get("title_line") and (not at or at >= cutoff):
            out.append(str(row["title_line"]))
    return out[-200:]


def title_repeats(title: str, earlier: list[str]) -> str | None:
    """Exact (normalized) match or near-identical wording to an earlier post's title."""
    key = _title_key(title)
    if not key:
        return "(empty)"
    for prev in earlier:
        pk = _title_key(prev)
        if not pk:
            continue
        if key == pk:
            return prev
        a, b = set(key.split()), set(pk.split())
        if len(a & b) / max(1, min(len(a), len(b))) >= 0.8 and min(len(a), len(b)) >= 2:
            return prev
    return None


def has_number(text: Any) -> bool:
    return bool(re.search(r"\d", str(text or "")))


def _clean_question(value: Any) -> str:
    q = growth_v2.clamp_chars(growth_v2._clean_line(value or ""), 80).strip()
    if not q:
        return ""
    return q if q.endswith("?") else q.rstrip(".!") + "?"


def sanitize_concept(raw: Any, brand: str = "") -> dict[str, Any]:
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, dict):
        return {}
    prompt = " ".join(str(raw.get("veo_prompt") or "").split())
    hook = growth_v2.clean_hook(raw.get("hook") or "", brand)
    if not prompt or not hook:
        return {}
    shots = []
    for beat in (raw.get("shot_list") or [])[:4]:
        if isinstance(beat, dict) and growth_v2._clean_line(beat.get("shows") or ""):
            shots.append({"t": str(beat.get("t") or "")[:10], "shows": growth_v2.clamp_chars(growth_v2._clean_line(beat.get("shows")), 160)})
        elif isinstance(beat, str) and growth_v2._clean_line(beat):
            shots.append({"t": "", "shows": growth_v2.clamp_chars(growth_v2._clean_line(beat), 160)})
    if shots and "shot list" not in prompt.lower():
        prompt += " Shot list, in order: " + " ".join(f"{b['t'] or 'next'}: {b['shows']}." for b in shots)
    if "16:9" not in prompt and "9:16" not in prompt:
        prompt += " Widescreen 16:9 framing." if style_aspect() == "16:9" else " Native vertical 9:16 framing."
    lines = raw.get("lines") or []
    if isinstance(lines, str):
        lines = [lines]
    return {
        "topic": growth_v2.clamp_chars(growth_v2._clean_line(raw.get("topic") or hook, brand), 80),
        "topic_key": str(raw.get("topic_key") or raw.get("topic") or hook)[:80],
        "veo_prompt": prompt[:1100],
        "shot_list": shots,
        "on_screen_hook": growth_v2.clamp_chars(growth_v2._clean_line(raw.get("on_screen_hook") or hook, brand), 40),
        "hook": hook,
        "lines": [growth_v2.clamp_chars(growth_v2._clean_line(l, brand), 110) for l in lines if growth_v2._clean_line(l, brand)][:2],
        "hashtags": style_hashtags(raw.get("hashtags")),
        "linkedin_angle": growth_v2.clamp_chars(growth_v2._clean_line(raw.get("linkedin_angle") or "", brand), 220),
        "comment_question": _clean_question(raw.get("comment_question")),
        "title_line": clean_title(raw.get("title_line"), brand),
        "commentary": [],
    }


async def plan_concept(record: dict[str, Any], tried: list[dict[str, Any]] | None = None, *, niche: str = "") -> dict[str, Any]:
    """New concept on a topic not used in the last 14 days and not tried earlier in this run."""
    from app.gemini import generate_text

    buyer_id = str(record.get("buyer_id") or "")
    brand = str(record.get("brand_name") or "")
    recent = recent_topics(buyer_id) + [c.get("topic") or "" for c in (tried or []) if c]
    avoid = list(dict.fromkeys(t for t in recent if t))[:60]
    titles = recent_titles(buyer_id) + [c.get("title_line") or "" for c in (tried or []) if c and c.get("title_line")]
    last_error = "no concept"
    for _ in range(3):
        prompt = CONCEPT_PROMPT.format(
            brand=brand or "the brand",
            website=record.get("website_url") or "",
            niche=niche or record.get("niche") or record.get("brand_description") or "the brand's audience",
            voice=record.get("brand_voice") or "plain, curious, confident",
            avoid=", ".join(avoid) or "none yet",
            avoid_titles="; ".join(titles[-40:]) or "none yet",
        )
        try:
            text = await generate_text(_api_key(record), prompt, models=CONCEPT_MODELS, budget_scope=buyer_id or "anon")
            concept = sanitize_concept(parse_json_object(text), brand)
        except gemini_budget.BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            last_error = f"concept generation failed: {exc}"
            continue
        if not concept:
            last_error = "concept missing prompt or hook"
            continue
        dup = is_recent_topic(concept, recent)
        if dup:
            last_error = f"topic {concept['topic']!r} too close to recent {dup!r}"
            avoid.append(concept["topic"])
            continue
        rep_title = title_repeats(concept.get("title_line") or "", titles)
        if rep_title:
            last_error = f"title {concept.get('title_line')!r} repeats earlier title {rep_title!r}"
            titles.append(concept.get("title_line") or "")
            continue
        if not has_number(concept["on_screen_hook"]):
            if has_number(concept["hook"]):
                concept["on_screen_hook"] = growth_v2.clamp_chars(concept["hook"], 40)
            else:
                last_error = f"on-screen hook for {concept['topic']!r} has no number"
                avoid.append(concept["topic"])
                continue
        return concept
    raise OriginalFailed(last_error)


# ---------------------------------------------------------------- Gemini Omni (8s, native 9:16, 720p)
async def generate_veo_clip(record: dict[str, Any], prompt: str, engine: str, dest: Path) -> dict[str, Any]:
    """One synchronous Gemini Omni interaction. No hidden fallback engine. Returns actual cost from usage."""
    key = _api_key(record)
    if not key:
        raise OriginalFailed("No Gemini API key on this account. Add your own Gemini key (setup) to make videos.")
    headers = {"x-goog-api-key": key, "content-type": "application/json"}
    gemini_budget.consume(str(record.get("buyer_id") or "anon"))
    async with httpx.AsyncClient(timeout=OMNI_TIMEOUT_SECONDS, follow_redirects=True) as client:
        try:
            resp = await client.post(f"{GEMINI_BASE}/interactions", headers=headers, json=omni_request_body(prompt, aspect_ratio=style_aspect()))
        except httpx.TimeoutException as exc:
            raise OriginalFailed(f"{engine}: timed out after {OMNI_TIMEOUT_SECONDS}s") from exc
        if resp.status_code != 200:
            raise OriginalFailed(f"{engine}: HTTP {resp.status_code} {resp.text[:300]}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise OriginalFailed(f"{engine}: unreadable Omni response") from exc
        usage = body.get("usage") or {}
        cost = omni_cost_from_usage(usage)
        if str(body.get("status") or "completed").lower() not in ("completed", "succeeded"):
            raise OriginalFailed(f"{engine}: Omni status {body.get('status')}")
        part = _omni_video_part(body)
        if not part:
            raise OriginalFailed(f"{engine}: no video returned (download missing)")
        try:
            if part.get("data"):
                content = base64.b64decode(part["data"])
            else:
                uri = part.get("uri") or part.get("file_uri") or ""
                video = await client.get(uri, headers={"x-goog-api-key": key}, timeout=300)
                if video.status_code != 200:
                    raise RuntimeError(f"HTTP {video.status_code}")
                content = video.content
        except Exception as exc:  # noqa: BLE001
            raise OriginalFailed(f"{engine}: video download failed {str(exc)[:200]}") from exc
        if len(content) < 10_000:
            raise OriginalFailed(f"{engine}: video download failed (empty file)")
        dest.write_bytes(content)
    return {"engine": engine, "model": OMNI_MODEL, "usage": usage, "actual_cost_usd": cost}


# ---------------------------------------------------------------- ffmpeg helpers
def _bin(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise OriginalFailed(f"{name} is not installed on this host")
    return path


def probe(path: Path) -> dict[str, Any]:
    res = subprocess.run([_bin("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                         capture_output=True, text=True, timeout=60)
    try:
        data = json.loads(res.stdout or "{}")
    except ValueError:
        data = {}
    out: dict[str, Any] = {"has_audio": False, "width": 0, "height": 0, "duration": 0.0}
    for s in data.get("streams") or []:
        if s.get("codec_type") == "video" and not out["width"]:
            out["width"], out["height"] = int(s.get("width") or 0), int(s.get("height") or 0)
        if s.get("codec_type") == "audio":
            out["has_audio"] = True
    try:
        out["duration"] = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        pass
    return out


def mean_volume_db(path: Path, start: float = 0.0, length: float = 1.0) -> float | None:
    try:
        res = subprocess.run([_bin("ffmpeg"), "-hide_banner", "-nostats", "-ss", f"{start:.2f}", "-t", f"{length:.2f}",
                              "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
                             capture_output=True, text=True, timeout=30)
    except Exception:  # noqa: BLE001
        return None
    found = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)", res.stderr or "")
    if not found:
        return None
    return -120.0 if found.group(1) == "-inf" else float(found.group(1))


def ensure_opening_sound(src: Path) -> tuple[Path, dict[str, Any]]:
    """If the first second is quieter than -38 dB (or silent), mix in a ~0.45s synthesized whoosh at t=0."""
    before = mean_volume_db(src)
    info: dict[str, Any] = {"opening_db_before": before, "sfx_added": False}
    if before is not None and before >= OPENING_SOUND_MIN_DB:
        info["opening_db_after"] = before
        return src, info
    dst = src.with_name(src.stem + "-sfx.mp4")
    sfx = ("anoisesrc=color=pink:amplitude=0.9:duration=0.45:sample_rate=48000,"
           "highpass=f=180,lowpass=f=6000,afade=t=in:d=0.03,afade=t=out:st=0.12:d=0.33,volume=1.6,"
           "aformat=channel_layouts=stereo[sfx]")
    if probe(src).get("has_audio"):
        filt = f"{sfx};[0:a]aformat=sample_rates=48000:channel_layouts=stereo[orig];[orig][sfx]amix=inputs=2:duration=first:normalize=0[a]"
        cmd = [_bin("ffmpeg"), "-y", "-i", str(src), "-filter_complex", filt, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", str(dst)]
    else:
        filt = f"{sfx};[sfx]apad[a]"
        cmd = [_bin("ffmpeg"), "-y", "-i", str(src), "-filter_complex", filt, "-map", "0:v", "-map", "[a]", "-shortest", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", str(dst)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0 or not dst.exists():
        logger.warning("opening sfx mix failed: %s", (res.stderr or "")[-300:])
        return src, info
    info.update({"sfx_added": True, "opening_db_after": mean_volume_db(dst)})
    return dst, info


def render_outputs(src: Path, stem: str, dest: Path, hook: str) -> dict[str, Any]:
    """1080x1920 (native 9:16, straight scale) + 1920x1080 (blur pad) with the hook burned in for
    the first 2.8s and a 0.2s audio fade at the end for a clean loop."""
    ffmpeg = _bin("ffmpeg")
    info = probe(src)
    dur = min(float(info.get("duration") or CLIP_SECONDS), 15.0)
    fade_st = max(0.0, dur - AUDIO_FADE_SECONDS)
    sw, sh = info.get("width") or 0, info.get("height") or 0
    native_vertical = bool(sw and sh and abs((sh / sw) - (16 / 9)) < 0.05)
    outputs = {}
    for label, (w, h) in (("vertical", (1080, 1920)), ("landscape", (1920, 1080))):
        png = growth_v2._text_png(dest / f"{stem}-{label}-hook.png", (w, h), hook.upper(),
                                  font_size=78 if h > w else 64, y_frac=0.14 if h > w else 0.08)
        if label == "vertical" and native_vertical:
            base = f"[0:v]scale={w}:{h}:flags=lanczos,setsar=1[base]"
        else:
            base = (f"[0:v]split=2[x][y];[x]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},gblur=sigma=22[bg];"
                    f"[y]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]")
        filt = f"{base};[base][1:v]overlay=0:0:enable='lt(t,{HOOK_SECONDS})',format=yuv420p[v]"
        out = dest / f"{stem}-{'9x16' if label == 'vertical' else '16x9'}.mp4"
        cmd = [ffmpeg, "-y", "-i", str(src), "-loop", "1", "-i", str(png), "-t", f"{dur:.2f}", "-filter_complex", filt,
               "-map", "[v]", "-map", "0:a?", "-af", f"afade=t=out:st={fade_st:.2f}:d={AUDIO_FADE_SECONDS}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", "30",
               "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        try:
            png.unlink()
        except OSError:
            pass
        if res.returncode != 0 or not out.exists():
            raise OriginalFailed(f"ffmpeg {label} render failed: {(res.stderr or '')[-400:]}")
        outputs[label] = out.name
    poster = dest / f"{stem}-poster.jpg"
    subprocess.run([ffmpeg, "-y", "-ss", "3.2", "-i", str(dest / outputs["landscape"]), "-frames:v", "1", "-q:v", "3", str(poster)],
                   capture_output=True, timeout=60)
    return {**outputs, "poster": poster.name if poster.exists() else "", "duration": dur,
            "native_vertical_source": native_vertical, "audio_fade_out_s": AUDIO_FADE_SECONDS,
            "source_width": sw, "source_height": sh}


def extract_frames(path: Path, count: int = 5, width: int = 512) -> list[bytes]:
    dur = probe(path).get("duration") or 0.0
    if dur <= 0:
        return []
    frames: list[bytes] = []
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(count):
            ts = dur * (i + 0.5) / count
            dst = Path(tmp) / f"f{i}.jpg"
            subprocess.run([_bin("ffmpeg"), "-v", "error", "-y", "-ss", f"{ts:.2f}", "-i", str(path), "-frames:v", "1",
                            "-vf", f"scale={width}:-2", "-q:v", "4", str(dst)], capture_output=True, timeout=30)
            if dst.exists() and dst.stat().st_size > 0:
                frames.append(dst.read_bytes())
    return frames


# ---------------------------------------------------------------- frame check (never publish a mismatch)
MATCH_PROMPT = """These {n} frames are sampled in order from an 8-second AI-generated video.
The post will be published with this on-screen hook and caption:
  title_line: "{title_line}"
  on_screen_hook: "{on_screen_hook}"
  hook: "{hook}"
  lines: {lines}
Judge strictly from what is VISIBLE. A caption claim fails if the frames do not show it.
Numbers and measurements are factual context, not visual claims: do NOT list them as mismatches just because
a camera cannot measure them. Only flag a number if the frames contradict the event it describes.
Return ONLY JSON:
  visible_summary: one sentence describing what the frames actually show,
  matches: true only if every visual claim in on_screen_hook, hook and lines is shown,
  mismatches: array of short strings (claims not shown),
  rewrite: if matches is false, a corrected caption that describes ONLY what is shown, still factual and
           curiosity-driven: {{"on_screen_hook": max 6 words AND must contain one factually correct number in digits,
           "title_line": 2-6 word punchy title for what is shown, no numbering,
           "hook": 8-10 words max 60 chars no emoji/hashtags, "lines": 1-2 lines under 100 chars}}; otherwise null."""


async def vision_json(record: dict[str, Any], prompt: str, frames: list[bytes]) -> dict[str, Any]:
    key = _api_key(record)
    if not key:
        raise OriginalFailed("Gemini API key is not configured (needed for the frame check).")
    gemini_budget.consume(str(record.get("buyer_id") or "anon"))
    parts: list[dict[str, Any]] = [{"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(f).decode()}} for f in frames]
    parts.append({"text": prompt})
    payload = {"contents": [{"parts": parts}], "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{GEMINI_BASE}/models/{VISION_MODEL}:generateContent",
                                 headers={"x-goog-api-key": key, "content-type": "application/json"}, json=payload)
    if resp.status_code >= 400:
        raise OriginalFailed(f"frame check: HTTP {resp.status_code} {resp.text[:300]}")
    data = resp.json()
    text = "".join(p.get("text", "") for p in ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts", []) if isinstance(p, dict))
    return parse_json_object(text)


async def verify_clip_matches(record: dict[str, Any], raw: Path, concept: dict[str, Any], *, force_mismatch: bool = False) -> dict[str, Any]:
    frames = await asyncio.to_thread(extract_frames, raw, 5)
    if not frames:
        raise OriginalFailed("caption check: could not extract frames")
    prompt = MATCH_PROMPT.format(n=len(frames), on_screen_hook=concept.get("on_screen_hook", ""),
                                 hook=concept.get("hook", ""), lines=json.dumps(concept.get("lines") or []),
                                 title_line=concept.get("title_line", ""))
    result = await vision_json(record, prompt, frames)
    if not isinstance(result, dict) or "matches" not in result:
        raise OriginalFailed("caption check: unusable verifier response")
    result["matches"] = result.get("matches") is True
    if force_mismatch:  # test hook: pretend the verifier rejected this caption
        result["matches"] = False
        result.setdefault("mismatches", [])
        result["mismatches"] = list(result.get("mismatches") or []) + ["(forced mismatch for testing)"]
        if not isinstance(result.get("rewrite"), dict):
            result["rewrite"] = {"on_screen_hook": "", "hook": "", "lines": []}
            result["rewrite"]["hook"] = growth_v2.clean_hook(result.get("visible_summary") or "", "")
            result["rewrite"]["lines"] = [str(result.get("visible_summary") or "")[:100]]
    result["frames"] = len(frames)
    return result


def apply_caption_rewrite(concept: dict[str, Any], rewrite: Any, brand: str = "", earlier_titles: list[str] | None = None) -> dict[str, Any] | None:
    if not isinstance(rewrite, dict):
        return None
    hook = growth_v2.clean_hook(rewrite.get("hook") or "", brand)
    lines = rewrite.get("lines") or []
    if isinstance(lines, str):
        lines = [lines]
    lines = [growth_v2.clamp_chars(growth_v2._clean_line(l, brand), 110) for l in lines if growth_v2._clean_line(l, brand)][:2]
    if not hook or not lines:
        return None
    on_screen = growth_v2.clamp_chars(growth_v2._clean_line(rewrite.get("on_screen_hook") or hook, brand), 40)
    if not has_number(on_screen) and has_number(concept.get("on_screen_hook")):
        num = re.search(r"\d[\d,.]*\s*(?:\u00b0\s*[CF]|%|[a-zA-Z/]+)?", concept["on_screen_hook"])
        if num:
            on_screen = growth_v2.clamp_chars(f"{num.group(0).strip()}: {on_screen}".strip(": "), 40)
    fixed = dict(concept)
    new_title = clean_title(rewrite.get("title_line"), brand)
    if new_title and not title_repeats(new_title, list(earlier_titles or [])):
        fixed["title_line"] = new_title
    fixed.update({"hook": hook, "lines": lines, "on_screen_hook": on_screen, "caption_rewritten_from_frames": True,
                  "original_caption": {"hook": concept.get("hook"), "lines": concept.get("lines"),
                                       "on_screen_hook": concept.get("on_screen_hook")}})
    return fixed


async def ensure_caption_matches(record: dict[str, Any], raw: Path, concept: dict[str, Any], *, force_mismatch: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """match -> keep; mismatch -> rewrite from frames and re-check; still failing -> raise (regenerate)."""
    brand = str(record.get("brand_name") or "")
    first = await verify_clip_matches(record, raw, concept, force_mismatch=force_mismatch)
    if first["matches"]:
        return concept, {"caption_check": "match", "visible_summary": first.get("visible_summary")}
    fixed = apply_caption_rewrite(concept, first.get("rewrite"), brand, recent_titles(str(record.get("buyer_id") or "")))
    if not fixed:
        raise OriginalFailed(f"caption mismatch, no usable rewrite: {first.get('mismatches')} | shown: {first.get('visible_summary')}")
    if not has_number(fixed.get("on_screen_hook")):
        raise OriginalFailed("caption rewrite dropped the number from the on-screen hook")
    second = await verify_clip_matches(record, raw, fixed)
    if not second["matches"]:
        raise OriginalFailed(f"caption mismatch after rewrite: {second.get('mismatches')} | shown: {second.get('visible_summary')}")
    return fixed, {"caption_check": "rewritten_from_frames", "visible_summary": second.get("visible_summary"),
                   "mismatches_fixed": first.get("mismatches")}


# ---------------------------------------------------------------- the retry ladder

async def produce_original(
    record: dict[str, Any],
    *,
    niche: str = "",
    force_veo_failures: int = 0,
    force_mismatch: bool = False,
    sleep=asyncio.sleep,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Returns (media, concept, info). Each failed attempt gets a NEW concept on a NEW topic and walks
    the Omni retry ladder (a second clip only fits the cap after an unbilled failure). Raises OriginalFailed only after every attempt failed.

    force_veo_failures / force_mismatch are test hooks for production dry-runs only."""
    from app.media import _public_record, buyer_media_dir

    buyer_id = str(record["buyer_id"])
    dest = buyer_media_dir(buyer_id)
    tried: list[dict[str, Any]] = []
    errors: list[str] = []
    ladder = engine_ladder()
    cap, spent, started = cost_cap(), 0.0, 0
    for i, engine in enumerate(ladder):
        veo_usd = clip_cost(engine)
        if spent + PLAN_USD + veo_usd + CHECK_USD > cap + 1e-9:
            errors.append(f"attempt {i + 1} {engine}: skipped, cost cap ${cap:.2f} (spent ${spent:.2f})")
            continue
        wait = backoff_seconds(started)
        if started and wait > 0:
            await sleep(wait)
        started += 1
        concept: dict[str, Any] = {}
        try:
            spent += PLAN_USD
            concept = await plan_concept(record, tried, niche=niche)
            stem = "orig-" + hashlib.sha256(f"{buyer_id}|{concept['topic']}|{time.time()}".encode()).hexdigest()[:10]
            raw = dest / f"{stem}-raw.mp4"
            if i < force_veo_failures:
                raise OriginalFailed(f"{engine}: forced Veo failure (test)")
            try:
                gen = await generate_veo_clip(record, concept["veo_prompt"], engine, raw)
            except Exception as gen_exc:
                if veo_failure_billed(gen_exc):
                    spent += veo_usd
                raise
            actual = gen.get("actual_cost_usd")
            spent += (min(veo_usd, float(actual)) if actual is not None else veo_usd) + CHECK_USD
            concept, check = await ensure_caption_matches(record, raw, concept, force_mismatch=force_mismatch and i == force_veo_failures)
            sounded, sound = await asyncio.to_thread(ensure_opening_sound, raw)
            rendered = await asyncio.to_thread(render_outputs, sounded, stem, dest, concept["on_screen_hook"])
            meta = {
                "source_url": "",
                "mock": False,
                "duration": rendered["duration"],
                "start": 0.0,
                "raw": raw.name,
                "vertical": rendered["vertical"],
                "landscape": rendered["landscape"],
                "poster": rendered["poster"],
                "media_source": "ai_generated",
                "origin": "original",
                "engine": gen["engine"],
                "model": gen["model"],
                "attempt": i + 1,
                "format": "single_8s",
                "cost_usd": round(spent, 4),
                "cost_cap_usd": cap,
                **{k: v for k, v in rendered.items() if k not in {"vertical", "landscape", "poster", "duration"}},
                **check,
                **sound,
                "failed_attempts": [{"topic": c.get("topic"), "error": e[:200]} for c, e in zip(tried, errors)],
            }
            (dest / f"{stem}.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")
            return _public_record(buyer_id, meta), concept, meta
        except gemini_budget.BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 — next attempt, new topic
            tried.append(concept or {"topic": f"(no concept, attempt {i + 1})", "topic_key": f"none-{i}"})
            errors.append(f"attempt {i + 1} {engine} [{concept.get('topic') if concept else '-'}]: {exc}")
            logger.error("original attempt %s/%s on %s failed: %s", i + 1, len(ladder), engine, exc)
    raise OriginalFailed(f"All {len(errors)} original video attempts failed: " + " | ".join(errors)[-1500:])


def copy_for_original(record: dict[str, Any], concept: dict[str, Any], platforms: list[str]) -> dict[str, Any]:
    brand = str(record.get("brand_name") or "")
    captions = growth_v2.build_captions(concept, platforms, brand=brand, website=str(record.get("website_url") or ""),
                                        locked=list(record.get("brand_hashtags") or []))
    copy = {
        "title": concept["hook"],
        "hook": concept["hook"],
        "on_screen_hook": concept["on_screen_hook"],
        "commentary": [],
        "topic": concept["topic"],
        "topic_key": concept.get("topic_key") or "",
        "topic_tags": [t.lstrip("#") for t in growth_v2.normalize_tags(concept.get("hashtags"), None, brand)],
        "locked_hashtags": list(record.get("brand_hashtags") or []),
        "captions": captions,
        "brand": brand,
        "source_url": "",
        "growth_v2": True,
        "origin": "original",
        "comment_question": concept.get("comment_question") or "",
        "title_line": concept.get("title_line") or "",
        "shot_list": concept.get("shot_list") or [],
        "caption_rewritten_from_frames": bool(concept.get("caption_rewritten_from_frames")),
        "original_caption": concept.get("original_caption"),
    }
    copy["youtube_title"] = growth_v2.youtube_title_for(copy, brand)
    return copy
