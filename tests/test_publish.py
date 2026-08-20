from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-publish-tests")

import pytest

from app import postproxy
from app.spine import publish_cut
from app.store import ensure_buyer, load_buyer, save_buyer


def _record():
    record = ensure_buyer("place-studio")
    record["postproxy_api_key"] = "pp-test"
    record["postproxy_profile_group_id"] = "grp_test"
    record["brand_name"] = "Bret Jenny"
    record["website_url"] = "https://6framestudio.com"
    record["platforms"] = ["facebook", "twitter", "google_business"]
    return save_buyer(record)


@pytest.mark.asyncio
async def test_publish_fills_facebook_page_and_gbp_location(monkeypatch) -> None:
    record = _record()
    copy = {
        "title": "Cut",
        "youtube_title": "Cut #Shorts",
        "captions": {
            "facebook": "fb body",
            "twitter": "x body",
            "google_business": "gbp body",
        },
    }
    media = {
        "vertical_url": "https://example.test/v.mp4",
        "landscape_url": "https://example.test/l.mp4",
        "poster_url": "https://example.test/poster.jpg",
    }
    calls: list[dict] = []

    async def fake_profiles(api_key, group=""):
        return {
            "data": [
                {"id": "prof_fb", "name": "Jim Reeves", "platform": "facebook"},
                {"id": "prof_x", "name": "6Frame Studios", "platform": "twitter"},
                {"id": "prof_gbp", "name": "Bret Jenny", "platform": "google_business"},
            ]
        }

    async def fake_placements(api_key, profile_id):
        if profile_id == "prof_fb":
            return {"data": [{"id": "page_jim", "name": "Jim Reeves"}]}
        if profile_id == "prof_gbp":
            return {"data": [{"id": "accounts/1/locations/9", "name": "Bret Jenny"}]}
        return {"data": []}

    async def fake_create(api_key, **kwargs):
        calls.append(kwargs)
        return {"id": "post_ok", "profiles": kwargs["profiles"]}

    monkeypatch.setattr(postproxy, "list_profiles", fake_profiles)
    monkeypatch.setattr(postproxy, "list_placements", fake_placements)
    monkeypatch.setattr(postproxy, "create_post", fake_create)

    result = await publish_cut(record, copy, media, mock=False, draft=True)
    assert len(result["posts"]) == 3
    assert all(item["ok"] for item in result["posts"])
    by_name = {}
    for item in calls:
        if item.get("platforms"):
            by_name[next(iter(item["platforms"]))] = item
    assert len(calls) == 3
    assert by_name["facebook"]["platforms"]["facebook"]["page_id"] == "page_jim"
    assert by_name["google_business"]["platforms"]["google_business"]["location_id"] == "accounts/1/locations/9"
    assert by_name["google_business"]["media"] == ["https://example.test/poster.jpg"]
    saved = load_buyer("place-studio")
    assert saved["facebook_page_id"] == "page_jim"
    assert saved["google_location_id"] == "accounts/1/locations/9"


@pytest.mark.asyncio
async def test_twitter_forbidden_retries_text_then_reconnect(monkeypatch) -> None:
    record = _record()
    record["platforms"] = ["twitter"]
    record = save_buyer(record)
    copy = {"title": "Cut", "youtube_title": "Cut #Shorts", "captions": {"twitter": "x body"}}
    media = {"landscape_url": "https://example.test/l.mp4", "vertical_url": "https://example.test/v.mp4"}
    sends = {"n": 0}

    async def fake_profiles(api_key, group=""):
        return {"data": [{"id": "prof_x", "name": "6Frame Studios", "platform": "twitter"}]}

    async def fake_create(api_key, **kwargs):
        sends["n"] += 1
        raise postproxy.PostProxyError("PostProxy 403: Twitter API Forbidden")

    async def fake_connect(api_key, group, platform, redirect):
        return {"url": "https://postproxy.dev/oauth/twitter-reconnect"}

    monkeypatch.setattr(postproxy, "list_profiles", fake_profiles)
    monkeypatch.setattr(postproxy, "create_post", fake_create)
    monkeypatch.setattr(postproxy, "initialize_connection", fake_connect)

    result = await publish_cut(record, copy, media, mock=False, draft=True)
    assert result["posts"][0]["ok"] is False
    assert result["posts"][0]["open_this_url"] == "https://postproxy.dev/oauth/twitter-reconnect"
    assert "reconnect" in result["posts"][0]["say_to_user"].lower()
    assert sends["n"] == 2


def test_pick_placement_prefers_pin_then_name() -> None:
    payload = {
        "data": [
            {"id": "page_a", "name": "Other Co"},
            {"id": "page_jim", "name": "Jim Reeves"},
        ]
    }
    picked = postproxy.pick_placement(payload, pinned_id="page_jim")
    assert picked["id"] == "page_jim"
    named = postproxy.pick_placement(payload, prefer_name="Jim Reeves")
    assert named["id"] == "page_jim"
    first = postproxy.pick_placement(payload)
    assert first["id"] == "page_a"
