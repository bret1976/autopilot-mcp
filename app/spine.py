from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from app.config import (
    DEFAULT_BRAND_VOICE,
    GEMINI_COPY_MODELS,
    GEMINI_SCAN_MODELS,
    MAX_CLIP_SECONDS,
    ONBOARD_PLATFORMS,
    public_base_url,
)
from app.gemini import generate_json, generate_json_with_sources
from app.hashtags import apply_hashtags, topic_tags
from app.media import (
    MediaError,
    cut_direct_clip,
    download_and_cut,
    is_direct_video,
    is_post_url,
    is_youtube,
    render_brand_clip,
    source_kind,
    source_priority,
)
from app.platforms import (
    effective_platforms,
    normalize_platform,
    split_batches,
    uses_default_platforms,
    youtube_title,
)
from app import postproxy
from app.proof import build_proof_dashboard
from app import run_guard
from app import yt_proxy
from app.store import public_config, set_last_run, update_setup

SCAN_PROMPT = """You are scanning live public web results for ORIGINAL video clips this brand can cut today.
{source_rules}{youtube_rule}
Every source_url must be a real post permalink you found in search results — never a profile,
search page, homepage, or invented ID.
Never return a URL from this failed list: {failed}
Do not invent a new generated film.
Do not recommend generating Veo, FAL, or Runway footage.
The website is the brand of record. Write as that brand, not 6Frame, unless the site is 6Frame.

Return JSON only:
{{
  "candidates": [
    {{
      "title": "short working title",
      "source_url": "https://...",
      "platform": "x|tiktok|vimeo|reddit|instagram|youtube|direct",
      "why": "one sentence on why this cut is moving",
      "topic_tags": ["TopicOne", "TopicTwo"],
      "suggested_start": 0,
      "suggested_duration": 45,
      "notes": "what to keep in the trim"
    }}
  ]
}}

Brand: {brand}
Website: {website}
Niche / filter: {niche}
Voice reminder: {voice}
"""

COPY_PROMPT = """Rewrite social copy for this cut in THIS brand's voice.
The website is the brand of record. Write as {brand} ({website}), not 6Frame unless they are 6Frame.
Never use: game-changer, revolutionize, unlock, next-level, crush, viral hack.
Do not restore 6Frame voice. Do not use thin meta copy.

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
    "twitter": "short body, room for 1-2 tags under 280",
    "google_business": "short local update, no hashtag dump, under 1500 characters"
  }}
}}
"""


SOURCE_RULES_DATACENTER = """The downloader runs on a datacenter IP. YouTube bot-walls it ("Sign in to confirm you're not a bot"),
so YouTube links almost never download. Reddit and Instagram often block it too.
Return up to 5 candidates, best first, in this source order:
  1. X / Twitter posts with native video: https://x.com/<user>/status/<id>
  2. Direct video files (.mp4) or Vimeo videos
  3. TikTok videos: https://www.tiktok.com/@user/video/<id>
  4. Reddit video posts (v.redd.it): https://www.reddit.com/r/<sub>/comments/<id>/...
  5. YouTube Shorts only as a last resort"""

SOURCE_RULES_WITH_PROXY = """YouTube originals download fine (residential pull). Prefer short, recent clips.
Return up to 5 candidates, best first, mixing sources:
  - X / Twitter posts with native video: https://x.com/<user>/status/<id>
  - YouTube Shorts or videos: https://www.youtube.com/shorts/<id> or https://www.youtube.com/watch?v=<id>
  - Direct video files (.mp4) or Vimeo videos
  - TikTok videos: https://www.tiktok.com/@user/video/<id>
  - Reddit video posts (v.redd.it): https://www.reddit.com/r/<sub>/comments/<id>/..."""


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


logger = logging.getLogger("spine")

GROUNDING_REDIRECT_HOST = "vertexaisearch.cloud.google.com"
SCAN_FIELDS = (
    "title",
    "source_url",
    "platform",
    "why",
    "topic_tags",
    "suggested_start",
    "suggested_duration",
    "notes",
)


async def _resolve_grounding_redirect(client: Any, uri: str) -> str:
    """Follow one grounding-api-redirect hop and return the real destination URL."""
    if not uri or GROUNDING_REDIRECT_HOST not in uri:
        return uri or ""
    for method in ("HEAD", "GET"):
        try:
            res = await client.request(method, uri)
            location = res.headers.get("location") or ""
            if location.startswith("http") and GROUNDING_REDIRECT_HOST not in location:
                return location
        except Exception:  # noqa: BLE001
            continue
    return ""


async def resolve_grounded_urls(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    """Real post permalinks (x.com/status, tiktok/video, reddit/comments, ...) cited by grounding."""
    if not sources:
        return []
    import httpx

    async with httpx.AsyncClient(timeout=8.0, follow_redirects=False) as client:
        resolved = await asyncio.gather(
            *(_resolve_grounding_redirect(client, item.get("uri") or "") for item in sources[:25])
        )
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item, url in zip(sources, resolved):
        if not url or url in seen or not is_post_url(url):
            continue
        seen.add(url)
        out.append({"source_url": url, "title": item.get("title") or "", "platform": source_kind(url)})
    return out


def _normalize_candidates(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    listed = raw.get("candidates") if isinstance(raw, dict) else None
    if isinstance(listed, list):
        items.extend(item for item in listed if isinstance(item, dict))
    if isinstance(raw, dict) and raw.get("source_url"):
        items.append({key: raw.get(key) for key in SCAN_FIELDS})
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        url = str(item.get("source_url") or "").strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        clean.append({**item, "source_url": url})
    return clean


def order_candidates(candidates: list[dict[str, Any]], *, avoid_youtube: bool = False) -> list[dict[str, Any]]:
    """Non-YouTube originals first (stable order otherwise). Drop YouTube once it bot-walled."""
    kept = [c for c in candidates if not (avoid_youtube and is_youtube(str(c.get("source_url") or "")))]
    return sorted(kept, key=lambda c: source_priority(str(c.get("source_url") or "")))


async def scan_trends(
    record: dict[str, Any],
    niche: str = "",
    mock: bool = False,
    exclude_urls: list[str] | None = None,
    avoid_youtube: bool = False,
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
    prompt = SCAN_PROMPT.format(
        niche=niche or default_niche,
        voice=voice,
        brand=brand,
        website=website,
        failed=failed,
        source_rules=SOURCE_RULES_WITH_PROXY if yt_proxy.configured() else SOURCE_RULES_DATACENTER,
        youtube_rule=(
            "\nYouTube is bot-walled right now: return NO YouTube links at all."
            if avoid_youtube
            else ""
        ),
    )
    grounded: list[dict[str, str]] = []
    try:
        raw, sources = await generate_json_with_sources(
            record.get("gemini_api_key") or "",
            prompt,
            models=GEMINI_SCAN_MODELS,
        )
        try:
            grounded = await resolve_grounded_urls(sources)
        except Exception:  # noqa: BLE001 — grounding links are a bonus, never fatal
            grounded = []
    except Exception:  # noqa: BLE001 — fall back to the plain grounded call
        raw = await generate_json(
            record.get("gemini_api_key") or "",
            prompt,
            grounded=True,
            models=GEMINI_SCAN_MODELS,
        )
    candidates = _normalize_candidates(raw)
    known = {c["source_url"] for c in candidates}
    for item in grounded:
        if item["source_url"] in known:
            continue
        known.add(item["source_url"])
        candidates.append(
            {
                "title": item.get("title") or (candidates[0].get("title") if candidates else "") or brand,
                "source_url": item["source_url"],
                "platform": item.get("platform") or "",
                "why": (candidates[0].get("why") if candidates else "") or "",
                "topic_tags": (candidates[0].get("topic_tags") if candidates else []) or [],
                "suggested_start": 0,
                "suggested_duration": 45,
                "notes": "Real post link recovered from Google Search grounding.",
                "grounded": True,
            }
        )
    skip = set(exclude_urls or [])
    candidates = order_candidates([c for c in candidates if c["source_url"] not in skip], avoid_youtube=avoid_youtube)
    top = candidates[0] if candidates else {}
    result: dict[str, Any] = {key: top.get(key) for key in SCAN_FIELDS}
    if not result.get("topic_tags"):
        result["topic_tags"] = []
    result["candidates"] = candidates
    result["grounded_urls"] = [item["source_url"] for item in grounded]
    return result


async def write_copy(record: dict[str, Any], scan: dict[str, Any], mock: bool = False) -> dict[str, Any]:
    platforms = effective_platforms(record)
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
    raw_captions = raw.get("captions") or {}
    for platform in platforms:
        name = normalize_platform(str(platform))
        body = raw_captions.get(name) or raw_captions.get(platform) or raw.get("title") or ""
        captions[name] = apply_hashtags(name, body, extras, locked=locked)
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
    force: bool = False,
) -> dict[str, Any]:
    key = record.get("postproxy_api_key") or ""
    group = record.get("postproxy_profile_group_id") or ""
    indexed: dict[str, dict[str, Any]] = {}
    if not mock:
        try:
            indexed = postproxy.index_profiles(await postproxy.list_profiles(key, group))
        except postproxy.PostProxyError:
            indexed = {}
    # Any profile in the group counts (an expired one still gets tried, so the
    # reconnect URL comes back instead of a silent skip).
    connected = set(indexed)
    platforms = effective_platforms(record, connected or None)
    skipped = (
        [name for name in ONBOARD_PLATFORMS if name not in platforms]
        if connected and uses_default_platforms(record)
        else []
    )
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

    # Soft-block duplicate live publishes (drafts are free to stage).
    if not draft:
        buyer_id = str(record.get("id") or record.get("buyer_id") or "")
        force_flag = bool(
            force
            or record.get("run_guard_force")
            or (isinstance(copy, dict) and copy.get("run_guard_force"))
        )
        guard = run_guard.check_publish(
            buyer_id=buyer_id,
            platforms=platforms,
            copy=copy if isinstance(copy, dict) else None,
            media=media if isinstance(media, dict) else None,
            force=force_flag,
        )
        if guard.get("blocked"):
            return {
                "ok": False,
                "mocked": False,
                "batches": batches,
                "posts": [],
                "blocked_by_run_guard": True,
                "run_guard": guard,
                "say_to_user": (
                    "Run Guard soft-blocked a duplicate live publish within the "
                    f"{run_guard.window_sec()}s window "
                    f"(reason={guard.get('reason')}). "
                    "Call status / last_run, or set run_guard_force to override."
                ),
            }

    posts = []

    for aspect, names, url in (
        ("9:16", batches["vertical_9x16"], media.get("vertical_url")),
        ("16:9", batches["landscape_16x9"], media.get("landscape_url")),
        ("image", batches.get("image_still") or [], media.get("poster_url") or media.get("landscape_url")),
    ):
        if not names:
            continue
        for name in names:
            name = normalize_platform(name)
            posts.append(
                await _publish_one(
                    record,
                    copy,
                    name,
                    aspect,
                    url or "",
                    indexed=indexed,
                    draft=draft,
                )
            )
    result = {"ok": True, "mocked": False, "batches": batches, "posts": posts}
    if skipped:
        result["skipped_not_connected"] = skipped
    posted = [item for item in posts if item.get("ok")]
    if not draft and posted:
        # Record even a partial send (e.g. Facebook 422, others live) so a re-run
        # cannot double-post to the platforms that already went out.
        try:
            run_guard.record_publish(
                buyer_id=str(record.get("id") or record.get("buyer_id") or ""),
                platforms=[item.get("platform") for item in posted if item.get("platform")] or platforms,
                copy=copy if isinstance(copy, dict) else None,
                media=media if isinstance(media, dict) else None,
            )
        except Exception:  # noqa: BLE001 — ledger must never change the post result
            pass
    if not draft and posts and len(posted) == len(posts):
        try:
            proof = await build_proof_dashboard(record, result, copy=copy, media=media, draft=False)
        except Exception:  # noqa: BLE001 — proof must never change the post result
            proof = None
        if proof and proof.get("url"):
            result["proof"] = proof
    return result


TIKTOK_CAPTION_MAX = 2200


async def _publish_one(
    record: dict[str, Any],
    copy: dict[str, Any],
    name: str,
    aspect: str,
    url: str,
    *,
    indexed: dict[str, dict[str, Any]],
    draft: bool,
) -> dict[str, Any]:
    key = record.get("postproxy_api_key") or ""
    group = record.get("postproxy_profile_group_id") or ""
    profile = indexed.get(name) or {}
    profile_ref = str(profile.get("id") or name)
    account = str(profile.get("name") or name)
    params, placement, pin_field = await _platform_params(record, copy, name, profile, key)
    media_urls = [url] if url else []
    if name == "google_business":
        media_urls = [url] if url else []
    captions = copy.get("captions") or {}
    body = captions.get(name) or ""
    if not body and name == "tiktok":
        # Copy written before TikTok was targeted: reuse the other vertical caption.
        body = captions.get("instagram") or captions.get("youtube") or ""
    body = body or copy.get("title") or ""
    if name == "tiktok":
        body = body[:TIKTOK_CAPTION_MAX]

    async def send(media_list: list[str]) -> Any:
        return await postproxy.create_post(
            key,
            body=body,
            profiles=[profile_ref],
            media=media_list,
            platforms={name: params} if params else None,
            profile_group_id=group,
            draft=draft,
        )

    try:
        result = await send(media_urls)
    except postproxy.PostProxyError as exc:
        if name == "twitter" and media_urls and postproxy.is_forbidden(exc):
            try:
                result = await send([])
                return {
                    "ok": True,
                    "aspect": aspect,
                    "platform": name,
                    "account": account,
                    "placement": placement,
                    "retried": "text_only",
                    "result": result,
                    "say_to_user": (
                        f"X rejected the video on {account}, so we posted the caption without the clip."
                    ),
                }
            except postproxy.PostProxyError as retry_exc:
                exc = retry_exc
        reconnect = None
        if postproxy.is_forbidden(exc) or name == "twitter":
            try:
                reconnect = await postproxy.initialize_connection(
                    key,
                    group,
                    "twitter" if name == "twitter" else name,
                    f"{record.get('public_base_url') or public_base_url()}/connected",
                )
            except postproxy.PostProxyError:
                reconnect = None
        return {
            "ok": False,
            "aspect": aspect,
            "platform": name,
            "account": account,
            "placement": placement,
            "error": str(exc),
            "open_this_url": (reconnect or {}).get("url") if isinstance(reconnect, dict) else None,
            "say_to_user": (
                f"{account} on {name} needs a reconnect in PostProxy. "
                "Open the URL and finish OAuth on that profile, then run again."
                if postproxy.is_forbidden(exc)
                else str(exc)
            ),
        }

    if pin_field and placement:
        update_setup(record["buyer_id"], {pin_field: placement})
        record[pin_field] = placement
    return {
        "ok": True,
        "aspect": aspect,
        "platform": name,
        "account": account,
        "placement": placement,
        "result": result,
    }


async def _platform_params(
    record: dict[str, Any],
    copy: dict[str, Any],
    name: str,
    profile: dict[str, Any],
    api_key: str,
) -> tuple[dict[str, Any], str | None, str | None]:
    if name == "youtube":
        return {"title": copy["youtube_title"], "privacy_status": "public"}, None, None
    if name == "instagram":
        return {"format": "reel"}, None, None
    if name == "tiktok":
        return {"format": "video"}, None, None
    if name == "facebook":
        picked = await _resolve_placement(
            api_key,
            profile,
            pinned=str(record.get("facebook_page_id") or ""),
            prefer_name=str(profile.get("name") or ""),
        )
        page_id = postproxy.placement_id(picked) if picked else ""
        params: dict[str, Any] = {"format": "reel"}
        if page_id:
            params["page_id"] = page_id
        return params, page_id or None, "facebook_page_id"
    if name == "google_business":
        picked = await _resolve_placement(
            api_key,
            profile,
            pinned=str(record.get("google_location_id") or ""),
            prefer_name=str(record.get("brand_name") or profile.get("name") or ""),
        )
        location_id = postproxy.placement_id(picked) if picked else ""
        params = {"format": "standard"}
        if location_id:
            params["location_id"] = location_id
        website = str(record.get("website_url") or "").strip()
        if website:
            params["cta_action_type"] = "LEARN_MORE"
            params["cta_url"] = website
        return params, location_id or None, "google_location_id"
    return {}, None, None


async def _resolve_placement(
    api_key: str,
    profile: dict[str, Any],
    *,
    pinned: str,
    prefer_name: str,
) -> dict[str, Any] | None:
    profile_id = str(profile.get("id") or "")
    if not profile_id:
        return {"id": pinned} if pinned else None
    try:
        payload = await postproxy.list_placements(api_key, profile_id)
    except postproxy.PostProxyError:
        return {"id": pinned} if pinned else None
    return postproxy.pick_placement(payload, pinned_id=pinned, prefer_name=prefer_name)


def _always_post() -> bool:
    return (os.environ.get("AUTOPILOT_ALWAYS_POST") or "1").strip().lower() not in {"0", "false", "no", "off"}


def _fallback_clip_urls() -> list[str]:
    raw = os.environ.get("FALLBACK_CLIP_URLS") or ""
    return [part.strip() for part in raw.split(",") if part.strip().startswith("http")]


def _brand_scan(record: dict[str, Any], source_url: str) -> dict[str, Any]:
    brand = str(record.get("brand_name") or "our brand")
    website = str(record.get("website_url") or "")
    return {
        "title": f"{brand} — what we build for founders",
        "source_url": source_url or website,
        "platform": "brand",
        "why": f"A direct note from {brand}: who we are and who we build with. Website: {website}",
        "topic_tags": [],
        "suggested_start": 0,
        "suggested_duration": 12,
        "notes": "Brand spotlight clip from the brand's own website art. Write as the brand, point to the website.",
        "scanned": False,
        "fallback": True,
    }


async def _fallback_media(record: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Hosted clip (FALLBACK_CLIP_URLS) first, then a brand clip rendered from the buyer's site."""
    errors: list[dict[str, str]] = []
    for url in _fallback_clip_urls():
        try:
            media = await asyncio.to_thread(cut_direct_clip, record["buyer_id"], url)
            return media, errors
        except MediaError as exc:
            errors.append({"url": url, "code": exc.code, "error": str(exc)[:300]})
    try:
        media = await asyncio.to_thread(render_brand_clip, record["buyer_id"], record)
        return media, errors
    except MediaError as exc:
        errors.append({"url": str(record.get("website_url") or ""), "code": exc.code, "error": str(exc)[:300]})
    return None, errors


MAX_SCANS = 3
MAX_DOWNLOADS = 6
CANDIDATE_BUDGET_SECONDS = 420


async def run_autopilot(
    record: dict[str, Any],
    *,
    niche: str = "",
    source_url: str = "",
    mock: bool = False,
    draft: bool = False,
    via: str = "manual",
) -> dict[str, Any]:
    """scan → pull an original → copy → publish.

    YouTube bot-walls the Railway IP, so non-YouTube originals (X, direct, Vimeo, TikTok,
    Reddit) are tried first; once YouTube walls, every later YouTube candidate is skipped
    without a download. If no original can be pulled, a live run still publishes a hosted
    or brand fallback clip instead of ending with nothing.
    """
    skipped: list[dict[str, str]] = []
    tried: set[str] = set()
    pinned = (source_url or "").strip()
    scan: dict[str, Any] = {}
    media: dict[str, Any] | None = None
    youtube_walled = False
    queue: list[dict[str, Any]] = []
    scans = 0
    downloads = 0
    started = time.monotonic()
    last_scan: dict[str, Any] = {}

    if pinned:
        queue.append(
            {
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
        )

    while media is None and downloads < MAX_DOWNLOADS:
        if time.monotonic() - started > CANDIDATE_BUDGET_SECONDS and not mock:
            break
        if not queue:
            if scans >= MAX_SCANS:
                break
            scans += 1
            try:
                found = await scan_trends(
                    record,
                    niche=niche,
                    mock=mock,
                    exclude_urls=sorted(tried),
                    avoid_youtube=youtube_walled,
                )
            except Exception as exc:  # noqa: BLE001 — a failed scan should not kill a live run
                skipped.append({"url": "", "code": "scan_failed", "error": str(exc)[:300]})
                continue
            last_scan = {k: v for k, v in found.items() if k != "candidates"}
            candidates = found.get("candidates")
            if not isinstance(candidates, list) or not candidates:
                candidates = [found] if found.get("source_url") else []
            for item in candidates:
                queue.append({**item, "scanned": True})
            # Keep YouTube in the queue (sorted last) so a walled host logs a skip, not a pull.
            queue = order_candidates(queue)
            if not queue:
                continue
        item = queue.pop(0)
        url = str(item.get("source_url") or "").strip()
        if not url or url in tried:
            continue
        tried.add(url)
        if youtube_walled and is_youtube(url):
            skipped.append(
                {
                    "url": url,
                    "code": "youtube_bot_wall_skip",
                    "error": "Skipped: YouTube already bot-walled this host in this run.",
                }
            )
            continue
        start = float(item.get("suggested_start") or 0)
        duration = float(item.get("suggested_duration") or MAX_CLIP_SECONDS)
        downloads += 1
        try:
            if is_direct_video(url) and not mock:
                media = await asyncio.to_thread(cut_direct_clip, record["buyer_id"], url, duration=duration)
            else:
                media = await asyncio.to_thread(
                    download_and_cut,
                    record["buyer_id"],
                    url,
                    start=start,
                    duration=duration,
                    mock=mock,
                )
            scan = {k: v for k, v in item.items() if k != "candidates"}
            scan.setdefault("scanned", True)
            break
        except MediaError as exc:
            if exc.code not in {"source_bot_check", "download_failed", "transcode_failed"}:
                raise
            if is_youtube(url) and exc.code == "source_bot_check":
                youtube_walled = True
            skipped.append({"url": url, "code": exc.code, "error": str(exc)})
            continue

    if media is None and not mock and _always_post():
        media, fallback_errors = await _fallback_media(record)
        skipped.extend(fallback_errors)
        if media is not None:
            scan = _brand_scan(record, str(media.get("source_url") or ""))
            if last_scan.get("topic_tags"):
                scan["topic_tags"] = list(last_scan.get("topic_tags") or [])
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
        "draft": draft,
        "pending_approval": bool(draft),
        "via": via,
        "scan": scan,
        "media": {k: v for k, v in media.items() if k != "raw"},
        "media_source": media.get("media_source") or "original",
        "copy": copy,
        "publish": published,
        "skipped_sources": skipped,
    }
    saved = set_last_run(record["buyer_id"], last_run)
    return {"ok": True, **last_run, "config": public_config(saved)}
