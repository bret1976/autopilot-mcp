from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request

from app.config import DEFAULT_PLATFORMS, public_base_url
from app import postproxy
from app.media import download_and_cut
from app.spine import publish_cut, run_autopilot, scan_trends, write_copy
from app.store import ensure_buyer, load_buyer, public_config, update_setup
from app.tokens import extract_token, verify_token

buyer_id_var: ContextVar[str | None] = ContextVar("buyer_id", default=None)

mcp = FastMCP(
    "6Frame Autopilot",
    instructions=(
        "Locked Autopilot spine for AI-filmmaking socials. "
        "Scan a viral original, download and trim it under 60s, write 6Frame-voice "
        "copy with guaranteed hashtags, publish through the buyer's PostProxy. "
        "Do not generate new Veo/FAL/Runway clips as the primary path. "
        "Brand voice, platforms, and niche filters are customizable. The spine is not."
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


@mcp.tool
async def setup(
    gemini_api_key: str | None = None,
    postproxy_api_key: str | None = None,
    postproxy_profile_group_id: str | None = None,
    brand_voice: str | None = None,
    platforms: list[str] | None = None,
    public_base_url: str | None = None,
) -> dict[str, Any]:
    """Save the buyer's own keys and studio settings. Secrets are never echoed back."""
    record = current_record()
    saved = update_setup(
        record["buyer_id"],
        {
            "gemini_api_key": gemini_api_key,
            "postproxy_api_key": postproxy_api_key,
            "postproxy_profile_group_id": postproxy_profile_group_id,
            "brand_voice": brand_voice,
            "platforms": platforms,
            "public_base_url": public_base_url,
        },
    )
    return {
        "ok": True,
        "message": "Setup saved. Keys are stored on this service and will not be printed again.",
        "config": public_config(saved),
    }


@mcp.tool
async def configure(
    gemini_api_key: str | None = None,
    postproxy_api_key: str | None = None,
    postproxy_profile_group_id: str | None = None,
    brand_voice: str | None = None,
    platforms: list[str] | None = None,
    public_base_url: str | None = None,
) -> dict[str, Any]:
    """Alias of setup."""
    return await setup(
        gemini_api_key=gemini_api_key,
        postproxy_api_key=postproxy_api_key,
        postproxy_profile_group_id=postproxy_profile_group_id,
        brand_voice=brand_voice,
        platforms=platforms,
        public_base_url=public_base_url,
    )


@mcp.tool
async def postproxy_status() -> dict[str, Any]:
    """Show connected socials on the buyer's PostProxy account."""
    record = current_record()
    if not record.get("postproxy_api_key"):
        return {
            "ok": False,
            "connected": False,
            "help": postproxy.connect_instructions(
                record.get("public_base_url") or public_base_url()
            ),
        }
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
    }


@mcp.tool(name="scan_trends")
async def scan_trends_tool(niche: str = "") -> dict[str, Any]:
    """Gemini-grounded scan for a viral AI-filmmaking original. Does not generate a new clip."""
    record = current_record()
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
    """Write 6Frame-voice captions and apply guaranteed hashtags per platform."""
    record = current_record()
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
    draft: bool = False,
) -> dict[str, Any]:
    """Publish through PostProxy in two batches: 9:16 Shorts and 16:9 landscape."""
    record = current_record()
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
    draft: bool = False,
) -> dict[str, Any]:
    """Run the locked spine: scan → download/trim → write copy + hashtags → publish."""
    record = current_record()
    return await run_autopilot(
        record,
        niche=niche,
        source_url=source_url,
        mock=mock,
        draft=draft,
    )


@mcp.tool
async def status() -> dict[str, Any]:
    """Show configuration (keys masked) and the last Autopilot run."""
    record = current_record()
    return {
        "ok": True,
        "config": public_config(record),
        "defaults": {"platforms": list(DEFAULT_PLATFORMS)},
        "last_run": record.get("last_run"),
    }


@mcp.tool
async def last_run() -> dict[str, Any]:
    """Alias of status focused on the last run."""
    record = current_record()
    return {"ok": True, "last_run": record.get("last_run"), "config": public_config(record)}
