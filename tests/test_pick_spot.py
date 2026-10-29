from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")

import uuid

import pytest

from app import postproxy
from app.mcp_server import bind_buyer, pick_posting_spot
from app.store import ensure_buyer, load_buyer, update_setup


def _buyer() -> str:
    buyer = f"pick-{uuid.uuid4().hex[:8]}"
    bind_buyer(buyer)
    ensure_buyer(buyer)
    update_setup(buyer, {"website_url": "https://acme.example", "brand_name": "Acme",
                         "gemini_api_key": "g", "postproxy_api_key": "p", "postproxy_profile_group_id": "grp"})
    return buyer


@pytest.mark.asyncio
async def test_list_and_save_pinterest_board(monkeypatch) -> None:
    buyer = _buyer()

    async def fake_profiles(key, group=""):
        return [{"id": "prof_pin", "platform": "pinterest"}]

    async def fake_placements(key, pid):
        assert pid == "prof_pin"
        return {"data": [{"id": "b1", "name": "Recipes"}, {"id": "b2", "name": "Travel"}]}

    monkeypatch.setattr(postproxy, "list_profiles", fake_profiles)
    monkeypatch.setattr(postproxy, "list_placements", fake_placements)
    listed = await pick_posting_spot("pinterest")
    assert listed["ok"] and [o["id"] for o in listed["options"]] == ["b1", "b2"]
    saved = await pick_posting_spot("pinterest", "Travel")
    assert saved["ok"] and load_buyer(buyer)["pinterest_board_id"] == "b2"


@pytest.mark.asyncio
async def test_telegram_not_connected_and_empty_help(monkeypatch) -> None:
    _buyer()

    async def none(key, group=""):
        return []

    monkeypatch.setattr(postproxy, "list_profiles", none)
    out = await pick_posting_spot("telegram")
    assert out["connected"] is False

    async def tg(key, group=""):
        return [{"id": "prof_tg", "platform": "telegram"}]

    async def empty(key, pid):
        return []

    monkeypatch.setattr(postproxy, "list_profiles", tg)
    monkeypatch.setattr(postproxy, "list_placements", empty)
    out = await pick_posting_spot("telegram")
    assert out["ok"] and out["options"] == [] and "admin" in out["say_to_user"]


@pytest.mark.asyncio
async def test_other_platforms_rejected() -> None:
    _buyer()
    out = await pick_posting_spot("linkedin")
    assert out["ok"] is False
