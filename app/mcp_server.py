from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request

from app.config import ONBOARD_PLATFORMS, public_base_url
from app import postproxy
from app.media import download_and_cut
from app.onboard import blocked, fetch_brand_from_website, readiness
from app.spine import publish_cut, run_autopilot, scan_trends, write_copy
from app.store import ensure_buyer, load_buyer, public_config, update_setup
from app.tokens import extract_token, verify_token

buyer_id_var: ContextVar[str | None] = ContextVar("buyer_id", default=None)

mcp = FastMCP(
    "TrendPilot",
    instructions=(
        "You are TrendPilot (also called Autopilot / the autoposting MCP). "
        "The buyer pasted a private MCP URL into Claude. The spine is locked: "
        "scan a viral original → download/trim under 60s → write THEIR brand copy → "
        "PostProxy to their socials. "
        "FIRST tool on every new chat: call onboard. "
        "If onboard.needs_setup is true, ASK the user every item in ask_the_user "
        "and read say_to_user almost verbatim. Do not scan, write, download, or publish "
        "until onboard.ready is true. "
        "Never invent API keys. Never use a shared Gemini or PostProxy key. "
        "Never enable mock mode. Never dump 6Frame stub clips. "
        "Brand comes from THEIR name + website (example: IAN Group / iangroup.ai). "
        "Default platforms: LinkedIn, X, Instagram, YouTube, Facebook. "
        "Daily run defaults to 8:00 AM PT after they are wired. "
        "First live run uses draft=true so they can check the cut."
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
    return load_buyer(buyer_id) or ensure_buyer(buyer_id)


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
    }


@mcp.tool
async def onboard() -> dict[str, Any]:
    """FIRST call when the user says TrendPilot, Autopilot, or autoposting.

    Returns the exact questions to ask (Gemini key, PostProxy key, profile group,
    brand + website) before any scan or post. Never skip this.
    """
    return _onboard_payload(current_record())


@mcp.tool
async def start() -> dict[str, Any]:
    """Alias of onboard. Use this or onboard at the start of every session."""
    return await onboard()


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
    public_base_url: str | None = None,
) -> dict[str, Any]:
    """Save the buyer's own keys and brand. Secrets are never echoed back.

    If website_url is set, TrendPilot pulls brand voice from that site.
    """
    record = current_record()
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
        "public_base_url": public_base_url,
    }
    saved = update_setup(record["buyer_id"], fields)
    if website_url and not brand_voice:
        fetched = await fetch_brand_from_website(website_url)
        if fetched.get("ok"):
            saved = update_setup(
                record["buyer_id"],
                {
                    "website_url": fetched.get("website_url"),
                    "brand_name": brand_name or fetched.get("brand_name"),
                    "brand_voice": fetched.get("brand_voice"),
                    "brand_hashtags": brand_hashtags or fetched.get("brand_hashtags"),
                },
            )
            extra = {"brand_from_website": {k: v for k, v in fetched.items() if k != "brand_voice"}}
        else:
            extra = {"brand_from_website": fetched}
    else:
        extra = {}
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
    public_base_url: str | None = None,
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
        public_base_url=public_base_url,
    )


@mcp.tool
async def set_brand_from_website(website_url: str, brand_name: str | None = None) -> dict[str, Any]:
    """Pull brand name + voice from the buyer's website. Does not invent keys."""
    fetched = await fetch_brand_from_website(website_url)
    if not fetched.get("ok"):
        return fetched
    saved = update_setup(
        current_record()["buyer_id"],
        {
            "website_url": fetched.get("website_url"),
            "brand_name": brand_name or fetched.get("brand_name"),
            "brand_voice": fetched.get("brand_voice"),
            "brand_hashtags": fetched.get("brand_hashtags"),
        },
    )
    payload = _onboard_payload(saved)
    payload["brand_from_website"] = {k: v for k, v in fetched.items() if k != "brand_voice"}
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
async def scan_trends_tool(niche: str = "") -> dict[str, Any]:
    """Live Gemini scan for a viral original in the buyer's brand niche. No mock. No new clip."""
    record = current_record()
    gate = blocked(record, need="scan")
    if gate:
        return gate
    scan = await scan_trends(record, niche=niche, mock=False)
    return {"ok": True, "scan": scan}


@mcp.tool
async def download_original(
    source_url: str,
    start: float = 0.0,
    duration: float = 59.0,
) -> dict[str, Any]:
    """Download the original with yt-dlp and cut two masters: 9:16 and 16:9, under 60s."""
    record = current_record()
    media = download_and_cut(
        record["buyer_id"],
        source_url,
        start=start,
        duration=duration,
        mock=False,
    )
    return {"ok": True, "media": media}


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
    copy = await write_copy(record, scan, mock=False)
    return {"ok": True, "copy": copy}


@mcp.tool
async def publish(
    title: str,
    captions: dict[str, str],
    vertical_url: str,
    landscape_url: str,
    topic_tags: list[str] | None = None,
    draft: bool = True,
) -> dict[str, Any]:
    """Publish through the buyer's PostProxy. First call should stay draft=true."""
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
    result = await publish_cut(record, copy, media, mock=False, draft=draft)
    return {"ok": True, **result}


@mcp.tool(name="run_autopilot")
async def run_autopilot_tool(
    niche: str = "",
    source_url: str = "",
    mock: bool = False,
    draft: bool = True,
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
    return await run_autopilot(
        record,
        niche=niche,
        source_url=source_url,
        mock=False,
        draft=draft,
    )


@mcp.tool
async def status() -> dict[str, Any]:
    """Show readiness, masked keys, brand, schedule, and the last run."""
    record = current_record()
    payload = _onboard_payload(record)
    payload["last_run"] = record.get("last_run")
    payload["defaults"]["platforms"] = list(record.get("platforms") or ONBOARD_PLATFORMS)
    return payload


@mcp.tool
async def last_run() -> dict[str, Any]:
    """Alias of status focused on the last run."""
    record = current_record()
    return {"ok": True, "last_run": record.get("last_run"), "config": public_config(record)}
