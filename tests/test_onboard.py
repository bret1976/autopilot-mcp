from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-tests")

import pytest

from app.mcp_server import bind_buyer, mcp, onboard, run_autopilot_tool, setup
from app.onboard import hashtags_from_name, readiness
from app.store import apply_owner_studio_brand, ensure_buyer, save_buyer


@pytest.mark.asyncio
async def test_onboard_and_run_ask_for_buyer_keys() -> None:
    bind_buyer("fresh-studio")
    ensure_buyer("fresh-studio")
    report = await onboard()
    assert report["needs_setup"] is True
    assert report["ready"] is False
    assert "gemini_api_key" in report["missing"]
    assert "postproxy_api_key" in report["missing"]
    assert "website_url" in report["missing"]
    assert "Gemini API key" in report["say_to_user"]
    assert "PostProxy" in report["say_to_user"]
    assert "6Frame stub" in report["say_to_user"]

    refused = await run_autopilot_tool()
    assert refused["ok"] is False
    assert refused["needs_setup"] is True
    assert refused.get("mocked") is False

    mocked = await run_autopilot_tool(mock=True)
    assert mocked["ok"] is False
    assert "Mock" in mocked["say_to_user"]


@pytest.mark.asyncio
async def test_setup_with_brand_marks_ready() -> None:
    bind_buyer("north-ready")
    ensure_buyer("north-ready")
    report = await setup(
        gemini_api_key="test-gemini-not-real",
        postproxy_api_key="test-postproxy-not-real",
        postproxy_profile_group_id="grp_test",
        brand_name="North Light",
        website_url="https://northlight.example",
        brand_voice="Write as North Light. Precise.",
        platforms=["linkedin", "twitter", "instagram", "youtube", "facebook"],
        daily_run_hour=8,
        daily_run_timezone="America/Los_Angeles",
    )
    assert report["ready"] is True
    assert report["needs_setup"] is False
    assert report["config"]["brand_name"] == "North Light"
    assert report["config"]["website_url"] == "https://northlight.example"
    assert report["config"]["daily_run_hour"] == 8
    assert "gemini_api_key" not in report["config"]
    assert report["config"]["gemini_key"]


def test_readiness_and_hashtag_from_name() -> None:
    empty = readiness({"gemini_api_key": "", "postproxy_api_key": ""})
    assert empty["ready"] is False
    assert hashtags_from_name("North Light") == ["#NorthLight"]


def test_owner_license_resets_ian_to_six_frame() -> None:
    record = ensure_buyer("bret-jenny")
    record["brand_name"] = "IAN Group"
    record["website_url"] = "https://iangroup.ai/"
    record = save_buyer(record)
    restored = apply_owner_studio_brand(record)
    assert restored["brand_name"] == "6Frame Studio"
    assert restored["website_url"] == "https://6framestudio.com"


@pytest.mark.asyncio
async def test_onboard_is_registered() -> None:
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {
        "onboard",
        "start",
        "set_brand_from_website",
        "setup",
        "run_autopilot",
        "set_automation",
        "approve_and_publish",
    }.issubset(names)
