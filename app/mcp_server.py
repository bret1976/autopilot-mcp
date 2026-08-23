from __future__ import annotations

import asyncio
from contextvars import ContextVar
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request

from app.automation import automation_public
from app.config import ONBOARD_PLATFORMS, public_base_url
from app import postproxy
from app.jobs import is_busy, load_job, spawn_job
from app.media import MediaError, download_and_cut
from app.onboard import blocked, fetch_brand_from_website, normalize_website, readiness
from app.proof import proof_from_last_run
from app.spine import publish_cut, run_autopilot, scan_trends, write_copy
from app.store import (
    apply_owner_studio_brand,
    clear_buyer_api_keys,
    ensure_buyer,
    load_buyer,
    public_config,
    reset_instance,
    save_buyer,
    should_start_new_instance,
    update_setup,
)
from app.tokens import extract_token, verify_token

buyer_id_var: ContextVar[str | None] = ContextVar("buyer_id", default=None)

mcp = FastMCP(
    "TrendPilot",
    instructions=(
        "You are TrendPilot. Use ONLY TrendPilot tools. "
        "Do not browse websites. Do not open about/manifesto/linkedin pages. "
        "Do not read workspace files or rules. Do not research the brand. "
        "Do not replace the scraped voice. Do not draft posts. Do not call status first. "
        "If the user already pasted a website, call onboard(website_url=that_url) immediately. "
        "Otherwise call onboard() and ask only for the website. "
        "Locked order, every company, every time: "
        "1) onboard(website_url) brands the homepage. Say: Company branded. "
        "2) Ask only the three APIs from ask_the_user (Gemini, PostProxy key, PostProxy profile group). "
        "3) After setup saves those keys, call run_autopilot(draft=false). Viral scan and live post. "
        "4) When the job is ok, send proof.url. "
        "5) ONLY AFTER the proof link, ask which days and which times they want automation "
        "(multiple times per day is fine). Then call set_automation("
        "enabled=true, require_approval=false, daily_run_hours=[...], daily_run_days=[...]). "
        "Never invent keys. Never mock. Never stage a draft. Never ask them to type publish. "
        "If download fails with source_bot_check, call run_autopilot again without source_url. "
        "run_autopilot returns started=true. Poll status until job.status is ok or error."
    ),
)


def bind_buyer(buyer_id: str) -> None:
    buyer_id_var.set(buyer_id)


def current_buyer_id() -> str:
    buyer_id = buyer_id_var.get()
    if buyer_id:
        return buyer_id
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        parsed = verify_token(extract_token(request))
        if parsed:
            return parsed.buyer_id
    except Exception:  # noqa: BLE001
        pass
    raise PermissionError("A signed buyer token is required.")


def current_record() -> dict[str, Any]:
    buyer_id = current_buyer_id()
    return apply_owner_studio_brand(load_buyer(buyer_id) or ensure_buyer(buyer_id))


def buyer_from_request(request: Request) -> str | None:
    parsed = verify_token(extract_token(request))
    return parsed.buyer_id if parsed else None


def _onboard_payload(record: dict[str, Any]) -> dict[str, Any]:
    report = readiness(record)
    return {
        "ok": report["ready"],
        "needs_setup": not report["ready"],
        "ready": report["ready"],
        "say_to_user": report["say_to_user"],
        "ask_the_user": report["ask_the_user"],
        "missing": report["missing"],
        "next_after_keys": report["next_after_keys"],
        "defaults": report["defaults"],
        "config": public_config(record),
        "automation": automation_public(record),
    }


@mcp.tool
async def onboard(website_url: str | None = None) -> dict[str, Any]:
    """FIRST call. If they already pasted a website, pass website_url.

    Wipes leftover companies. Brands that site. Then ask only for the three APIs.
    Do not browse the site yourself.
    """
    buyer_id = current_buyer_id()
    ensure_buyer(buyer_id)
    reset_instance(buyer_id)
    if website_url and normalize_website(website_url):
        return await set_brand_from_website(website_url)
    return _onboard_payload(load_buyer(buyer_id) or ensure_buyer(buyer_id))


@mcp.tool
async def start(website_url: str | None = None) -> dict[str, Any]:
    """Alias of onboard. Pass website_url if they already pasted the site."""
    return await onboard(website_url)


@mcp.tool
async def setup(
    gemini_api_key: str | None = None,
    postproxy_api_key: str | None = None,
    postproxy_profile_group_id: str | None = None,
    brand_name: str | None = None,
    website_url: str | None = None,
    brand_voice: str | None = None,
    brand_hashtags: list[str] | None = None,
    platforms: list[str] | None = None,
    daily_run_hour: int | None = None,
    daily_run_timezone: str | None = None,
    automation_enabled: bool | None = None,
    require_approval: bool | None = None,
    public_base_url: str | None = None,
    facebook_page_id: str | None = None,
    google_location_id: str | None = None,
    reset_api_keys: bool = False,
) -> dict[str, Any]:
    """Save the buyer's own keys and brand. Secrets are never echoed back.

    If website_url is set, TrendPilot pulls brand voice from that site.
    facebook_page_id and google_location_id pin PostProxy placements.
    automation_enabled / require_approval are optional; prefer set_automation.
    reset_api_keys clears Gemini and PostProxy so onboard asks for the three APIs again.
    """
    record = current_record()
    if reset_api_keys:
        record = clear_buyer_api_keys(record["buyer_id"])
    if website_url:
        incoming = normalize_website(website_url)
        previous = normalize_website(str(record.get("website_url") or ""))
        if incoming and previous and incoming.rstrip("/") != previous.rstrip("/"):
            record = reset_instance(record["buyer_id"])
    fields: dict[str, Any] = {
        "gemini_api_key": gemini_api_key,
        "postproxy_api_key": postproxy_api_key,
        "postproxy_profile_group_id": postproxy_profile_group_id,
        "brand_name": brand_name,
        "website_url": website_url,
        "brand_voice": brand_voice,
        "brand_hashtags": brand_hashtags,
        "platforms": platforms,
        "daily_run_hour": daily_run_hour,
        "daily_run_timezone": daily_run_timezone,
        "automation_enabled": automation_enabled,
        "require_approval": require_approval,
        "public_base_url": public_base_url,
        "facebook_page_id": facebook_page_id,
        "google_location_id": google_location_id,
    }
    saved = update_setup(record["buyer_id"], fields)
    extra: dict[str, Any] = {}
    if website_url:
        fetched = await fetch_brand_from_website(website_url)
        if fetched.get("ok"):
            keep_voice = bool(brand_voice and len(brand_voice.strip()) > 400)
            saved = update_setup(
                record["buyer_id"],
                {
                    "website_url": fetched.get("website_url"),
                    "brand_name": brand_name or fetched.get("brand_name"),
                    "brand_voice": brand_voice if keep_voice else fetched.get("brand_voice"),
                    "brand_hashtags": brand_hashtags or fetched.get("brand_hashtags"),
                },
            )
            extra = {"brand_from_website": {k: v for k, v in fetched.items() if k != "brand_voice"}}
        else:
            extra = {"brand_from_website": fetched}
    payload = _onboard_payload(saved)
    payload.update(extra)
    payload["message"] = (
        "Setup saved. Keys stay on this service and will not be printed again."
        if payload["ready"]
        else payload["say_to_user"]
    )
    return payload


@mcp.tool
async def configure(
    gemini_api_key: str | None = None,
    postproxy_api_key: str | None = None,
    postproxy_profile_group_id: str | None = None,
    brand_name: str | None = None,
    website_url: str | None = None,
    brand_voice: str | None = None,
    brand_hashtags: list[str] | None = None,
    platforms: list[str] | None = None,
    daily_run_hour: int | None = None,
    daily_run_timezone: str | None = None,
    automation_enabled: bool | None = None,
    require_approval: bool | None = None,
    public_base_url: str | None = None,
    facebook_page_id: str | None = None,
    google_location_id: str | None = None,
) -> dict[str, Any]:
    """Alias of setup."""
    return await setup(
        gemini_api_key=gemini_api_key,
        postproxy_api_key=postproxy_api_key,
        postproxy_profile_group_id=postproxy_profile_group_id,
        brand_name=brand_name,
        website_url=website_url,
        brand_voice=brand_voice,
        brand_hashtags=brand_hashtags,
        platforms=platforms,
        daily_run_hour=daily_run_hour,
        daily_run_timezone=daily_run_timezone,
        automation_enabled=automation_enabled,
        require_approval=require_approval,
        public_base_url=public_base_url,
        facebook_page_id=facebook_page_id,
        google_location_id=google_location_id,
    )


@mcp.tool
async def set_brand_from_website(website_url: str, brand_name: str | None = None) -> dict[str, Any]:
    """Brand the pasted website. Do not browse extra pages. This tool scrapes the homepage."""
    record = current_record()
    incoming = normalize_website(website_url)
    previous = normalize_website(str(record.get("website_url") or ""))
    if should_start_new_instance(record) or (
        incoming and previous and incoming.rstrip("/") != previous.rstrip("/")
    ):
        record = reset_instance(record["buyer_id"])
    fetched = await fetch_brand_from_website(website_url)
    if not fetched.get("ok"):
        return fetched
    saved = update_setup(
        record["buyer_id"],
        {
            "website_url": fetched.get("website_url"),
            "brand_name": brand_name or fetched.get("brand_name"),
            "brand_voice": fetched.get("brand_voice"),
            "brand_hashtags": fetched.get("brand_hashtags"),
        },
    )
    payload = _onboard_payload(saved)
    payload["brand_from_website"] = {k: v for k, v in fetched.items() if k != "brand_voice"}
    if payload.get("needs_setup"):
        payload["message"] = (
            f"Company branded ({saved.get('brand_name') or fetched.get('brand_name')}). "
            "Onboarding next — paste your three APIs: Gemini API key, PostProxy API key, "
            "and PostProxy profile group id."
        )
    return payload


@mcp.tool
async def postproxy_status() -> dict[str, Any]:
    """Show connected socials on the buyer's PostProxy account."""
    record = current_record()
    gate = blocked(record, need="publish")
    if gate:
        return gate
    profiles = await postproxy.list_profiles(
        record["postproxy_api_key"],
        record.get("postproxy_profile_group_id") or "",
    )
    return {"ok": True, "profiles": profiles, "config": public_config(record)}


@mcp.tool
async def postproxy_connect(
    platform: str,
    redirect_url: str | None = None,
    profile_group_id: str | None = None,
) -> dict[str, Any]:
    """Return the PostProxy OAuth URL so the buyer can connect a social on THEIR account."""
    record = current_record()
    gate = blocked(record, need="publish")
    if gate:
        return gate
    group = profile_group_id or record.get("postproxy_profile_group_id") or ""
    redirect = redirect_url or f"{record.get('public_base_url') or public_base_url()}/connected"
    data = await postproxy.initialize_connection(
        record.get("postproxy_api_key") or "",
        group,
        platform,
        redirect,
    )
    return {
        "ok": True,
        "platform": platform,
        "open_this_url": data.get("url") if isinstance(data, dict) else None,
        "raw": data,
        "help": postproxy.connect_instructions(redirect.rsplit("/", 1)[0]),
        "say_to_user": f"Open this URL to connect {platform} on YOUR PostProxy account.",
    }


@mcp.tool(name="scan_trends")
async def scan_trends_tool(niche: str = "", exclude_urls: list[str] | None = None) -> dict[str, Any]:
    """Live Gemini scan for a viral original in the buyer's brand niche. No mock. No new clip."""
    record = current_record()
    gate = blocked(record, need="scan")
    if gate:
        return gate
    buyer_id = record["buyer_id"]

    async def _job() -> dict[str, Any]:
        bind_buyer(buyer_id)
        scan = await scan_trends(load_buyer(buyer_id) or record, niche=niche, mock=False, exclude_urls=exclude_urls)
        return {"ok": True, "scan": scan}

    return spawn_job(buyer_id, "scan_trends", _job, {"niche": niche})


@mcp.tool
async def download_original(
    source_url: str,
    start: float = 0.0,
    duration: float = 59.0,
) -> dict[str, Any]:
    """Download the original with yt-dlp and cut two masters: 9:16 and 16:9, under 60s."""
    record = current_record()
    buyer_id = record["buyer_id"]

    async def _job() -> dict[str, Any]:
        bind_buyer(buyer_id)
        try:
            media = await asyncio.to_thread(
                download_and_cut,
                buyer_id,
                source_url,
                start=start,
                duration=duration,
                mock=False,
            )
        except MediaError as exc:
            return {
                "ok": False,
                "step": "download",
                "code": exc.code,
                "source_url": source_url,
                "say_to_user": str(exc),
            }
        return {"ok": True, "media": media}

    return spawn_job(buyer_id, "download_original", _job, {"source_url": source_url})


@mcp.tool(name="write_copy")
async def write_copy_tool(
    title: str,
    why: str = "",
    notes: str = "",
    topic_tags: list[str] | None = None,
    source_url: str = "",
) -> dict[str, Any]:
    """Write captions in the buyer's brand voice. Not 6Frame unless they are 6Frame."""
    record = current_record()
    gate = blocked(record, need="scan")
    if gate:
        return gate
    scan = {
        "title": title,
        "why": why,
        "notes": notes,
        "topic_tags": topic_tags or [],
        "source_url": source_url,
    }
    buyer_id = record["buyer_id"]

    async def _job() -> dict[str, Any]:
        bind_buyer(buyer_id)
        copy = await write_copy(load_buyer(buyer_id) or record, scan, mock=False)
        return {"ok": True, "copy": copy}

    return spawn_job(buyer_id, "write_copy", _job)


@mcp.tool
async def publish(
    title: str,
    captions: dict[str, str],
    vertical_url: str,
    landscape_url: str,
    topic_tags: list[str] | None = None,
    draft: bool = True,
) -> dict[str, Any]:
    """Publish through the buyer's PostProxy. Live first posts use run_autopilot draft=false."""
    record = current_record()
    gate = blocked(record, need="publish")
    if gate:
        return gate
    copy = {
        "title": title,
        "youtube_title": title if "#shorts" in title.lower() else f"{title} #Shorts",
        "topic_tags": topic_tags or [],
        "captions": captions,
    }
    media = {"vertical_url": vertical_url, "landscape_url": landscape_url}
    buyer_id = record["buyer_id"]

    async def _job() -> dict[str, Any]:
        bind_buyer(buyer_id)
        result = await publish_cut(load_buyer(buyer_id) or record, copy, media, mock=False, draft=draft)
        return {"ok": True, **result}

    return spawn_job(buyer_id, "publish", _job)


@mcp.tool(name="run_autopilot")
async def run_autopilot_tool(
    niche: str = "",
    source_url: str = "",
    mock: bool = False,
    draft: bool = False,
) -> dict[str, Any]:
    """Live spine: scan → download/trim → write THEIR copy → publish. Mock is always rejected."""
    if mock:
        return {
            "ok": False,
            "mocked": False,
            "needs_setup": True,
            "say_to_user": (
                "Mock mode is off. I will not dump stub clips. "
                "Enter your Gemini API key and PostProxy API key, then run again."
            ),
        }
    record = current_record()
    gate = blocked(record, need="run")
    if gate:
        return gate
    buyer_id = record["buyer_id"]

    async def _job() -> dict[str, Any]:
        bind_buyer(buyer_id)
        try:
            return await run_autopilot(
                load_buyer(buyer_id) or record,
                niche=niche,
                source_url=source_url,
                mock=False,
                draft=draft,
            )
        except MediaError as exc:
            return {
                "ok": False,
                "step": "download",
                "code": exc.code,
                "say_to_user": str(exc),
            }

    return spawn_job(buyer_id, "run_autopilot", _job, {"source_url": source_url, "niche": niche})


@mcp.tool
async def set_automation(
    enabled: bool | None = None,
    require_approval: bool | None = None,
    daily_run_hour: int | None = None,
    daily_run_hours: list[int] | str | None = None,
    daily_run_days: list[str] | str | None = None,
    daily_run_timezone: str | None = None,
) -> dict[str, Any]:
    """Set days and times for recurring scan-and-post. Call this ONLY after the proof link.

    daily_run_hours: one or more hours, e.g. [8, 17] or "8,17".
    daily_run_days: everyday, weekdays, weekends, or ["mon","wed","fri"].
    require_approval=false posts at those times with no extra click.
    """
    record = current_record()
    gate = blocked(record, need="run")
    if gate and enabled is True:
        return gate
    saved = update_setup(
        record["buyer_id"],
        {
            "automation_enabled": enabled,
            "require_approval": False if enabled is True and require_approval is None else require_approval,
            "daily_run_hour": daily_run_hour,
            "daily_run_hours": daily_run_hours,
            "daily_run_days": daily_run_days,
            "daily_run_timezone": daily_run_timezone,
        },
    )
    auto = automation_public(saved)
    if auto["automation_enabled"]:
        if auto["require_approval"]:
            message = (
                f"Automation is on {auto['schedule']}. "
                "Each run will scan and download, then wait. Call approve_and_publish to post."
            )
        else:
            message = (
                f"Automation is on {auto['schedule']}. "
                "Each run will scan, download the original, and post with no approval click."
            )
    else:
        message = "Automation is off. One-off runs still work via run_autopilot. The MCP URL is unchanged."
    payload = _onboard_payload(saved)
    payload["ok"] = True
    payload["message"] = message
    payload["say_to_user"] = message
    return payload


@mcp.tool
async def approve_and_publish() -> dict[str, Any]:
    """Post the last staged automation draft. Used when require_approval is on."""
    record = current_record()
    gate = blocked(record, need="publish")
    if gate:
        return gate
    last = record.get("last_run") or {}
    copy = last.get("copy") or {}
    media = last.get("media") or {}
    if not last.get("pending_approval"):
        return {
            "ok": False,
            "say_to_user": (
                "Nothing is waiting for approval. "
                "Enable set_automation with require_approval=true, wait for the daily run, "
                "or call run_autopilot with draft=true first."
            ),
            "last_run": last,
        }
    if not copy or not (media.get("vertical_url") or media.get("landscape_url")):
        return {
            "ok": False,
            "say_to_user": "The staged run has no copy or media to publish.",
            "last_run": last,
        }
    buyer_id = record["buyer_id"]

    async def _job() -> dict[str, Any]:
        bind_buyer(buyer_id)
        fresh = load_buyer(buyer_id) or record
        published = await publish_cut(fresh, copy, media, mock=False, draft=False)
        current = load_buyer(buyer_id) or fresh
        run = dict(current.get("last_run") or last)
        run["draft"] = False
        run["pending_approval"] = False
        run["publish"] = published
        current["last_run"] = run
        saved = save_buyer(current)
        return {"ok": True, "publish": published, "last_run": run, "config": public_config(saved)}

    return spawn_job(buyer_id, "approve_and_publish", _job)


@mcp.tool
async def status() -> dict[str, Any]:
    """Show readiness, masked keys, brand, schedule, and the last run."""
    record = current_record()
    payload = _onboard_payload(record)
    payload["last_run"] = record.get("last_run")
    payload["job"] = load_job(record["buyer_id"])
    payload["busy"] = is_busy(record["buyer_id"])
    payload["defaults"]["platforms"] = list(record.get("platforms") or ONBOARD_PLATFORMS)
    return payload


@mcp.tool
async def proof_link() -> dict[str, Any]:
    """After a live Autopilot post, return the proof link. Does not change setup or posting."""
    record = current_record()
    built = await proof_from_last_run(record)
    if built.get("ok") and built.get("url"):
        last = dict(record.get("last_run") or {})
        last["proof"] = {k: v for k, v in built.items() if k != "posts"}
        record["last_run"] = last
        save_buyer(record)
        built["say_to_user"] = (
            f"All confirmed socials are on this proof dashboard: {built['url']}"
        )
    return built


@mcp.tool
async def last_run() -> dict[str, Any]:
    """Alias of status focused on the last run."""
    record = current_record()
    return {
        "ok": True,
        "last_run": record.get("last_run"),
        "automation": automation_public(record),
        "config": public_config(record),
    }
