from __future__ import annotations

from typing import Any

import httpx

from app.config import POSTPROXY_API_BASE


class PostProxyError(RuntimeError):
    pass


def _headers(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


async def _request(
    method: str,
    path: str,
    api_key: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    if not api_key:
        raise PostProxyError("PostProxy API key is not configured. Call setup first.")
    url = f"{POSTPROXY_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.request(
            method,
            url,
            headers=_headers(api_key),
            json=json,
            params=params,
        )
    if response.status_code >= 400:
        raise PostProxyError(f"PostProxy {response.status_code}: {response.text[:500]}")
    if not response.content:
        return {}
    return response.json()


def as_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "profiles", "placements", "items"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def placement_id(row: dict[str, Any]) -> str:
    for key in ("id", "page_id", "location_id", "resource_name", "resourceName"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def pick_placement(
    payload: Any,
    *,
    pinned_id: str = "",
    prefer_name: str = "",
) -> dict[str, Any] | None:
    rows = as_rows(payload)
    if not rows:
        return None
    pinned = (pinned_id or "").strip()
    if pinned:
        for row in rows:
            if placement_id(row) == pinned or str(row.get("name") or "") == pinned:
                return row
    needle = (prefer_name or "").strip().lower()
    if needle:
        for row in rows:
            name = str(row.get("name") or "").lower()
            if needle in name or name in needle:
                return row
    return rows[0]


def is_forbidden(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("403", "forbidden", "unauthorized", "reconnect"))


async def list_profiles(api_key: str, profile_group_id: str = "") -> Any:
    params = {"profile_group_id": profile_group_id} if profile_group_id else None
    return await _request("GET", "/api/profiles", api_key, params=params)


async def list_profile_groups(api_key: str) -> Any:
    return await _request("GET", "/api/profile_groups", api_key)


async def list_placements(api_key: str, profile_id: str) -> Any:
    return await _request("GET", f"/api/profiles/{profile_id}/placements", api_key)


def index_profiles(payload: Any) -> dict[str, dict[str, Any]]:
    from app.platforms import normalize_platform

    out: dict[str, dict[str, Any]] = {}
    for row in as_rows(payload):
        platform = normalize_platform(str(row.get("platform") or row.get("network") or ""))
        if platform:
            out[platform] = row
    return out


async def initialize_connection(
    api_key: str,
    profile_group_id: str,
    platform: str,
    redirect_url: str,
) -> Any:
    if not profile_group_id:
        raise PostProxyError(
            "postproxy_profile_group_id is missing. Save it with setup, "
            "or pass it to postproxy_connect."
        )
    return await _request(
        "POST",
        f"/api/profile_groups/{profile_group_id}/initialize_connection",
        api_key,
        json={"platform": platform, "redirect_url": redirect_url},
    )


async def get_post(api_key: str, post_id: str) -> Any:
    post_id = str(post_id or "").strip()
    if not post_id:
        raise PostProxyError("Post id is required.")
    return await _request("GET", f"/api/posts/{post_id}", api_key)


def result_post_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("id", "post_id"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        nested = payload.get("data")
        if isinstance(nested, dict):
            return result_post_id(nested)
    return ""


def platform_outcomes(payload: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if not isinstance(payload, dict):
        return rows
    for key in ("platforms", "results", "items"):
        found = payload.get(key)
        if isinstance(found, list):
            rows.extend(row for row in found if isinstance(row, dict))
    nested = payload.get("data")
    if isinstance(nested, dict):
        rows.extend(platform_outcomes(nested))
    elif isinstance(nested, list):
        rows.extend(row for row in nested if isinstance(row, dict) and row.get("platform"))
    return rows


async def create_post(
    api_key: str,
    *,
    body: str,
    profiles: list[str],
    media: list[str],
    platforms: dict[str, Any] | None = None,
    profile_group_id: str = "",
    draft: bool = False,
) -> Any:
    payload: dict[str, Any] = {
        "post": {"body": body, "draft": draft},
        "profiles": profiles,
        "media": media,
    }
    if platforms:
        payload["platforms"] = platforms
    params = {"profile_group_id": profile_group_id} if profile_group_id else None
    return await _request("POST", "/api/posts", api_key, json=payload, params=params)


def connect_instructions(public_base: str) -> dict[str, Any]:
    return {
        "provider": "PostProxy",
        "docs": "https://postproxy.dev/reference/overview/",
        "steps": [
            "Create a PostProxy account at https://postproxy.dev and copy your API key.",
            "Create a profile group for this studio (or use the default group id).",
            "Call setup with postproxy_api_key and postproxy_profile_group_id.",
            "Call postproxy_connect for each network you want live.",
            "Open the returned URL and finish OAuth on the buyer's own accounts.",
        ],
        "redirect_hint": f"{public_base}/connected",
        "note": "Socials stay on the buyer's PostProxy account. This MCP never hosts those logins.",
    }
