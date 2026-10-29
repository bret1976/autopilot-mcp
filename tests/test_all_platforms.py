from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")

import uuid

import pytest

from app import postproxy, spine
from app.catalog import PLATFORM_CATALOG, clamp_caption
from app.config import CHANNEL_CHOICES, OWNER_BUYER_ID
from app.mcp_server import bind_buyer, choose_channels
from app.platforms import aspect_for, effective_platforms, normalize_platform, split_batches
from app.store import ensure_buyer, update_setup

POSTPROXY_PLATFORMS = {"facebook", "instagram", "tiktok", "linkedin", "youtube", "twitter",
                       "threads", "pinterest", "bluesky", "telegram", "google_business"}


def test_catalog_covers_every_postproxy_platform() -> None:
    assert set(PLATFORM_CATALOG) == POSTPROXY_PLATFORMS
    assert {c for c, _ in CHANNEL_CHOICES} == POSTPROXY_PLATFORMS
    for name in POSTPROXY_PLATFORMS:
        assert aspect_for(name) in {"9:16", "16:9", "image"}
    assert split_batches(sorted(POSTPROXY_PLATFORMS))["unknown"] == []
    assert normalize_platform("bsky") == "bluesky"


def test_caption_limits() -> None:
    long = "word " * 1000
    assert len(clamp_caption("twitter", long)) <= 280
    assert len(clamp_caption("bluesky", long)) <= 300
    assert len(clamp_caption("threads", long)) <= 500
    assert clamp_caption("linkedin", "short") == "short"


def test_owner_defaults_unchanged() -> None:
    owner = {"buyer_id": OWNER_BUYER_ID}
    got = effective_platforms(owner, {"youtube", "tiktok", "instagram", "linkedin", "threads"})
    assert "youtube" not in got


@pytest.mark.asyncio
async def test_choose_any_platform_reads_postproxy_live(monkeypatch) -> None:
    buyer = f"all-{uuid.uuid4().hex[:8]}"
    bind_buyer(buyer)
    ensure_buyer(buyer)
    update_setup(buyer, {"website_url": "https://acme.example", "brand_name": "Acme",
                         "gemini_api_key": "g", "postproxy_api_key": "p", "postproxy_profile_group_id": "grp"})

    async def fake_profiles(key, group=""):
        return [{"platform": "threads"}, {"platform": "bluesky"}, {"platform": "pinterest"}]

    monkeypatch.setattr(postproxy, "list_profiles", fake_profiles)
    ask = await choose_channels()
    assert {c["id"] for c in ask["choices"] if c["connected"]} == {"threads", "bluesky", "pinterest"}
    assert len(ask["choices"]) == 11
    done = await choose_channels(["threads", "bluesky", "pinterest", "telegram", "google business"])
    assert done["ok"] and set(done["channels"]) == {"threads", "bluesky", "pinterest", "telegram", "google_business"}


@pytest.mark.asyncio
async def test_placement_params(monkeypatch) -> None:
    async def fake_resolve(api_key, profile, *, pinned, prefer_name):
        return {"id": pinned} if pinned else None

    monkeypatch.setattr(spine, "_resolve_placement", fake_resolve)
    rec = {"pinterest_board_id": "b1", "telegram_chat_id": "c1"}
    assert (await spine._platform_params(rec, {}, "pinterest", {}, "k"))[0] == {"board_id": "b1"}
    assert (await spine._platform_params(rec, {}, "telegram", {}, "k"))[0] == {"chat_id": "c1"}
    # LinkedIn with no organization saved posts to the personal profile, as today.
    assert (await spine._platform_params({}, {}, "linkedin", {}, "k"))[0] == {}
    assert (await spine._platform_params({"linkedin_organization_id": "o1"}, {}, "linkedin", {}, "k"))[0] == {"organization_id": "o1"}
    assert (await spine._platform_params({}, {}, "threads", {}, "k"))[0] == {}


@pytest.mark.asyncio
async def test_pinterest_without_board_is_skipped_not_posted(monkeypatch) -> None:
    async def fake_resolve(*a, **k):
        return None

    async def boom(*a, **k):
        raise AssertionError("should not post")

    monkeypatch.setattr(spine, "_resolve_placement", fake_resolve)
    monkeypatch.setattr(postproxy, "create_post", boom)
    out = await spine._publish_one({"buyer_id": "x"}, {"captions": {}}, "pinterest", "9:16", "https://v",
                                   indexed={}, draft=True)
    assert out["ok"] is False and "board_id" in out["error"]
