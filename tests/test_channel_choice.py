from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")

import uuid

import pytest

from app import postproxy
from app.config import OWNER_BUYER_ID
from app.mcp_server import bind_buyer, choose_channels
from app.platforms import effective_platforms
from app.store import ensure_buyer, load_buyer, update_setup
from app.walkthrough import infer_step


def _keys(buyer: str) -> dict:
    ensure_buyer(buyer)
    return update_setup(buyer, {"website_url": "https://acme.example", "brand_name": "Acme",
                                "gemini_api_key": "g", "postproxy_api_key": "p", "postproxy_profile_group_id": "grp"})


@pytest.mark.asyncio
async def test_channels_asked_right_after_postproxy_and_saved(monkeypatch) -> None:
    buyer = f"chan-{uuid.uuid4().hex[:8]}"
    bind_buyer(buyer)
    record = _keys(buyer)
    assert infer_step(record) == "ask_channels"

    async def fake_profiles(key, group=""):
        return [{"platform": "tiktok"}, {"platform": "instagram"}, {"platform": "youtube"}]

    monkeypatch.setattr(postproxy, "list_profiles", fake_profiles)
    ask = await choose_channels()
    assert ask["ok"] is False
    assert [c["id"] for c in ask["choices"]] [:5] == ["youtube", "tiktok", "instagram", "linkedin", "twitter"]
    assert {c["id"] for c in ask["choices"] if c["connected"]} == {"youtube", "tiktok", "instagram"}

    done = await choose_channels(["TikTok", "x", "youtube shorts", "myspace"])
    assert done["ok"] and done["channels"] == ["tiktok", "twitter", "youtube"]
    saved = load_buyer(buyer)
    assert saved["channels_chosen"] is True
    assert infer_step(saved) == "ask_schedule"
    # posting only goes to the chosen channels, even if more are connected
    assert effective_platforms(saved, {"tiktok", "twitter", "youtube", "instagram", "linkedin"}) == ["tiktok", "twitter", "youtube"]


def test_existing_accounts_keep_their_platforms() -> None:
    old = {"buyer_id": "old", "platforms": ["linkedin", "youtube"], "platforms_set_by_user": True}
    assert effective_platforms(old) == ["linkedin", "youtube"]
    passed = {"buyer_id": "old2", "schedule_set_by_user": True, "brand_name": "x",
              "gemini_api_key": "g", "postproxy_api_key": "p", "postproxy_profile_group_id": "grp"}
    assert infer_step(passed) != "ask_channels"
    assert "youtube" in effective_platforms({"buyer_id": "someone"})


def test_owner_default_has_no_youtube() -> None:
    owner = {"buyer_id": OWNER_BUYER_ID}
    assert "youtube" not in effective_platforms(owner)
    assert "youtube" not in effective_platforms(owner, {"youtube", "tiktok", "instagram"})
    picked = {"buyer_id": OWNER_BUYER_ID, "platforms": ["youtube"], "platforms_set_by_user": True}
    assert effective_platforms(picked) == ["youtube"]  # only if he picks it himself
