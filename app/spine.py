from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config import (
    DEFAULT_BRAND_VOICE,
    GEMINI_COPY_MODELS,
    GEMINI_SCAN_MODELS,
    MAX_CLIP_SECONDS,
    ONBOARD_PLATFORMS,
)
from app.gemini import generate_json
from app.hashtags import apply_hashtags, topic_tags
from app.media import MediaError, download_and_cut
from app.platforms import split_batches, youtube_title
from app import postproxy
from app.store import public_config, set_last_run

SCAN_PROMPT = """You are scanning live public web results for one ORIGINAL clip this brand can cut today.
The downloader runs on a datacenter IP. Long YouTube talks (TEDx, podcasts, news) get bot-walled.
Prefer a SHORT original (under 90s) in this order: TikTok, Instagram Reel, direct .mp4, YouTube Short.
Never return a URL from this failed list: {failed}
Do not invent a new generated film.
Do not recommend generating Veo, FAL, or Runway footage.
Do not write as 6Frame Studio unless this brand is 6Frame.

Return JSON only:
{{
  "title": "short working title",
  "source_url": "https://...",
  "platform": "tiktok|instagram|youtube",
  "why": "one sentence on why this cut is moving",
  "topic_tags": ["TopicOne", "TopicTwo"],
  "suggested_start": 0,
  "suggested_duration": 45,
  "notes": "what to keep in the trim"
}}

Brand: {brand}
Website: {website}
Niche / filter: {niche}
Voice reminder: {voice}
"""

COPY_PROMPT = """Rewrite social copy for this cut in THIS brand's voice — not 6Frame unless they are 6Frame.
Never use: game-changer, revolutionize, unlock, next-level, crush, viral hack.

Brand: {brand}
Website: {website}
Voice:
{voice}

Title: {title}
Why it is moving: {why}
Notes: {notes}
Topic tags (ideas only): {tags}

Return JSON only:
{{
  "title": "youtube title without hashtags except we will add #Shorts later",
  "topic_tags": ["tag", "tag"],
  "captions": {{
    "instagram": "caption body without hashtags",
    "tiktok": "...",
    "youtube": "...",
    "facebook": "...",
    "linkedin": "...",
    "twitter": "short body, room for 1-2 tags under 280"
  }}
}}
"""


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


async def scan_trends(
    record: dict[str, Any],
    niche: str = "",
    mock: bool = False,
    exclude_urls: list[str] | None = None,
) -> dict[str, Any]:
    failed = ", ".join(exclude_urls or []) or "(none)"
    if mock:
        return {
            "title": "Night tungsten / Kling street cut",
            "source_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "platform": "youtube",
            "why": "The original already has the light. We cut it, we do not remake it.",
            "topic_tags": ["Kling", "NightDrive"],
            "suggested_start": 3,
            "suggested_duration": 48,
            "notes": "Keep the lamp flare. Lose the talking-head open.",
            "mocked": True,
        }
    voice = record.get("brand_voice") or DEFAULT_BRAND_VOICE
    brand = record.get("brand_name") or "the buyer's brand"
    website = record.get("website_url") or ""
    default_niche = f"content that fits {brand}" + (f" ({website})" if website else "")
    return await generate_json(
        record.get("gemini_api_key") or "",
        SCAN_PROMPT.format(
            niche=niche or default_niche,
            voice=voice,
            brand=brand,
            website=website,
            failed=failed,
        ),
        grounded=True,
        models=GEMINI_SCAN_MODELS,
    )


async def write_copy(record: dict[str, Any], scan: dict[str, Any], mock: bool = False) -> dict[str, Any]:
    platforms = record.get("platforms") or list(ONBOARD_PLATFORMS)
    extras = topic_tags(scan.get("topic_tags") or [])
    locked = tuple(record.get("brand_hashtags") or ())
    if mock:
        raw = {
            "title": scan.get("title") or "Studio cut",
            "topic_tags": extras or ["Kling", "NightDrive"],
            "captions": {
                "instagram": "The original already had the light. We kept the flare and lost the rest.",
                "tiktok": "Not a remake. A trim of the clip that is already moving.",
                "youtube": "Cut from the viral original. Under a minute.",
                "facebook": "A 9:16 cut from the original — same lamp, shorter breath.",
                "linkedin": "Operators keep the model they already pay for. The spine is the product.",
                "twitter": "The original. Cut under 60. Posted.",
            },
        }
    else:
        raw = await generate_json(
            record.get("gemini_api_key") or "",
            COPY_PROMPT.format(
                voice=record.get("brand_voice") or DEFAULT_BRAND_VOICE,
                brand=record.get("brand_name") or "the buyer's brand",
                website=record.get("website_url") or "",
                title=scan.get("title") or "",
                why=scan.get("why") or "",
                notes=scan.get("notes") or "",
                tags=", ".join(extras) or (record.get("brand_name") or "the brand"),
            ),
            grounded=False,
            models=GEMINI_COPY_MODELS,
        )
    extras = topic_tags(raw.get("topic_tags") or extras)
    captions: dict[str, str] = {}
    for platform in platforms:
        body = (raw.get("captions") or {}).get(platform) or raw.get("title") or ""
        captions[platform] = apply_hashtags(platform, body, extras, locked=locked)
    return {
        "title": raw.get("title") or scan.get("title") or "Studio cut",
        "youtube_title": youtube_title(raw.get("title") or scan.get("title") or "Studio cut"),
        "topic_tags": extras,
        "locked_hashtags": list(locked),
        "captions": captions,
    }


async def publish_cut(
    record: dict[str, Any],
    copy: dict[str, Any],
    media: dict[str, Any],
    *,
    mock: bool = False,
    draft: bool = False,
) -> dict[str, Any]:
    platforms = record.get("platforms") or list(ONBOARD_PLATFORMS)
    batches = split_batches(platforms)
    if mock:
        return {
            "mocked": True,
            "batches": batches,
            "posts": [
                {
                    "aspect": "9:16",
                    "profiles": batches["vertical_9x16"],
                    "media": media.get("vertical_url"),
                },
                {
                    "aspect": "16:9",
                    "profiles": batches["landscape_16x9"],
                    "media": media.get("landscape_url"),
                },
            ],
        }

    posts = []
    key = record.get("postproxy_api_key") or ""
    group = record.get("postproxy_profile_group_id") or ""
    for aspect, names, url in (
        ("9:16", batches["vertical_9x16"], media.get("vertical_url")),
        ("16:9", batches["landscape_16x9"], media.get("landscape_url")),
    ):
        if not names:
            continue
        # One PostProxy call per platform so captions and YouTube title stay correct.
        for name in names:
            platform_params: dict[str, Any] = {}
            if name == "youtube":
                platform_params["youtube"] = {
                    "title": copy["youtube_title"],
                    "privacy_status": "public",
                }
            if name == "instagram":
                platform_params["instagram"] = {"format": "reel"}
            if name == "facebook":
                platform_params["facebook"] = {"format": "reel"}
            if name == "tiktok":
                platform_params["tiktok"] = {"format": "video"}
            result = await postproxy.create_post(
                key,
                body=copy["captions"].get(name, ""),
                profiles=[name],
                media=[url] if url else [],
                platforms=platform_params or None,
                profile_group_id=group,
                draft=draft,
            )
            posts.append({"aspect": aspect, "platform": name, "result": result})
    return {"mocked": False, "batches": batches, "posts": posts}


async def run_autopilot(
    record: dict[str, Any],
    *,
    niche: str = "",
    source_url: str = "",
    mock: bool = False,
    draft: bool = False,
) -> dict[str, Any]:
    skipped: list[dict[str, str]] = []
    pinned = (source_url or "").strip()
    scan: dict[str, Any] = {}
    media: dict[str, Any] | None = None
    url = ""
    for attempt in range(3):
        exclude = [item["url"] for item in skipped]
        if pinned and attempt == 0:
            scan = {
                "title": pinned,
                "source_url": pinned,
                "platform": "",
                "why": "",
                "topic_tags": [],
                "suggested_start": 0,
                "suggested_duration": MAX_CLIP_SECONDS,
                "notes": "Pinned source_url — scan_trends skipped",
                "scanned": False,
            }
            url = pinned
        else:
            scan = await scan_trends(record, niche=niche, mock=mock, exclude_urls=exclude)
            url = str(scan.get("source_url") or "")
            scan["scanned"] = True
        if not url:
            break
        if url in exclude:
            pinned = ""
            continue
        start = float(scan.get("suggested_start") or 0)
        duration = float(scan.get("suggested_duration") or MAX_CLIP_SECONDS)
        try:
            media = download_and_cut(
                record["buyer_id"],
                url,
                start=start,
                duration=duration,
                mock=mock,
            )
            break
        except MediaError as exc:
            if exc.code not in {"source_bot_check", "download_failed"}:
                raise
            skipped.append({"url": url, "code": exc.code, "error": str(exc)})
            pinned = ""
            continue
    if media is None:
        last = skipped[-1]["error"] if skipped else "scan_trends did not return a source_url"
        raise MediaError(
            last
            + " Tried another original automatically. Call run_autopilot again without a source_url.",
            code=skipped[-1]["code"] if skipped else "download_failed",
        )
    copy = await write_copy(record, scan, mock=mock)
    published = await publish_cut(record, copy, media, mock=mock, draft=draft)
    last_run = {
        "at": _stamp(),
        "mocked": mock,
        "scan": scan,
        "media": {k: v for k, v in media.items() if k != "raw"},
        "copy": copy,
        "publish": published,
        "skipped_sources": skipped,
    }
    saved = set_last_run(record["buyer_id"], last_run)
    return {"ok": True, **last_run, "config": public_config(saved)}
