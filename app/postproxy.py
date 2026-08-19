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


async def list_profiles(api_key: str, profile_group_id: str = "") -> Any:
    params = {"profile_group_id": profile_group_id} if profile_group_id else None
    return await _request("GET", "/api/profiles", api_key, params=params)


async def list_profile_groups(api_key: str) -> Any:
    return await _request("GET", "/api/profile_groups", api_key)


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
