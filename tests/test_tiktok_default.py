from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-tiktok-tests")

import pytest

from app import postproxy, run_guard
from app.config import LEGACY_ONBOARD_PLATFORMS, ONBOARD_PLATFORMS
from app.platforms import effective_platforms
from app.spine import publish_cut
from app.store import ensure_buyer, save_buyer, update_setup

# Mirrors PostProxy group 4MFZmD on 2026-10-10: TikTok connected, no Facebook.
GROUP = {
    "data": [
        {"id": "OLUaBP", "name": "6Frame Studio", "platform": "instagram", "status": "active"},
        {"id": "zkUpep", "name": "6framestudio", "platform": "tiktok", "status": "active"},
        {"id": "ZDUZJm", "name": "6Frame Studios", "platform": "twitter", "status": "active"},
        {"id": "2rU2mL", "name": "Bret Jenny", "platform": "youtube", "status": "active"},
        {"id": "4pU2oM", "name": "Bret", "platform": "linkedin", "status": "active"},
    ]
}


def test_default_platforms_include_tiktok() -> None:
    assert "tiktok" in ONBOARD_PLATFORMS
    assert "tiktok" not in LEGACY_ONBOARD_PLATFORMS
    assert "tiktok" in effective_platforms({"platforms": list(LEGACY_ONBOARD_PLATFORMS)})
    assert "tiktok" in effective_platforms({})


def test_user_picked_platforms_are_kept() -> None:
    picked = {"platforms": ["linkedin", "twitter"]}
    assert effective_platforms(picked, {"tiktok", "linkedin", "twitter"}) == ["linkedin", "twitter"]
    explicit_legacy = {"platforms": list(LEGACY_ONBOARD_PLATFORMS), "platforms_set_by_user": True}
    assert "tiktok" not in effective_platforms(explicit_legacy, {"tiktok"})


def test_setup_marks_platforms_as_picked() -> None:
    ensure_buyer("tt-setup")
    saved = update_setup("tt-setup", {"platforms": ["linkedin"]})
    assert saved["platforms_set_by_user"] is True
    assert effective_platforms(saved) == ["linkedin"]


@pytest.mark.asyncio
async def test_legacy_default_license_posts_to_connected_tiktok(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    record = ensure_buyer("tt-legacy")
    record.update(
        postproxy_api_key="pp-test",
        postproxy_profile_group_id="4MFZmD",
        platforms=list(LEGACY_ONBOARD_PLATFORMS),
    )
    record = save_buyer(record)
    copy = {
        "title": "Guardrails",
        "youtube_title": "Guardrails #Shorts",
        # Copy written before TikTok was targeted has no tiktok caption.
        "captions": {p: f"{p} body" for p in LEGACY_ONBOARD_PLATFORMS},
    }
    media = {"vertical_url": "https://e.test/v.mp4", "landscape_url": "https://e.test/l.mp4"}
    calls: list[dict] = []

    async def fake_profiles(api_key, group=""):
        return GROUP

    async def fake_create(api_key, **kwargs):
        calls.append(kwargs)
        return {"id": f"post_{len(calls)}"}

    monkeypatch.setattr(postproxy, "list_profiles", fake_profiles)
    monkeypatch.setattr(postproxy, "create_post", fake_create)
    monkeypatch.setattr(run_guard, "check_publish", lambda **_: {"blocked": False})
    monkeypatch.setattr(run_guard, "record_publish", lambda **_: None)

    result = await publish_cut(record, copy, media, mock=False, draft=True)

    sent = {call["profiles"][0]: call for call in calls}
    assert set(sent) == {"OLUaBP", "zkUpep", "ZDUZJm", "2rU2mL", "4pU2oM"}
    tiktok = sent["zkUpep"]
    assert tiktok["platforms"] == {"tiktok": {"format": "video"}}
    assert tiktok["media"] == ["https://e.test/v.mp4"]
    assert tiktok["body"] == "instagram body"
    assert result["skipped_not_connected"] == ["facebook"]
    assert all(post["ok"] for post in result["posts"])
