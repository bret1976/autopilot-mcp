from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-tests")

import pytest

from app.mcp_server import bind_buyer, mcp, onboard, run_autopilot_tool, setup
from app.onboard import brand_from_html, brand_name_from_host, hashtags_from_name, readiness
from app.store import apply_owner_studio_brand, ensure_buyer, load_buyer, save_buyer

CORY_HTML = """
<html><head>
  <title>Cory Warfield | Executive Coaching For Signal-Makers</title>
  <meta name="description" content="A wildly strategic executive coaching and consulting website concept for Cory Warfield: personal branding, keynotes, cohorts, and consulting."/>
</head><body>
  <h1>Turn your executive chaos into a public signal people actually follow.</h1>
  <p>Cory Warfield helps leaders package their expertise, expand their network, sharpen their AI-era point of view, and walk into bigger rooms with a story that sells before the pitch deck opens.</p>
  <p>Instagram — Daily signal, personality, public presence, and CoryConnects-in-motion.</p>
  <p>Build the point of view. Package the proof. Activate the network.</p>
</body></html>
"""


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
    assert report["missing"][0] == "website_url"
    assert "Gemini API key" in report["say_to_user"]
    assert "PostProxy" in report["say_to_user"]
    assert "three APIs" in report["say_to_user"]

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


@pytest.mark.asyncio
async def test_brand_then_asks_for_three_apis() -> None:
    bind_buyer("brand-first")
    ensure_buyer("brand-first")
    after_brand = await setup(
        brand_name="Cory Connects",
        website_url="https://coryconnects.example",
        brand_voice="Write as Cory Connects.",
    )
    assert after_brand["ready"] is False
    assert after_brand["needs_setup"] is True
    assert after_brand["config"]["brand_name"] == "Cory Connects"
    assert after_brand["missing"] == [
        "gemini_api_key",
        "postproxy_api_key",
        "postproxy_profile_group_id",
    ]
    assert "Brand is set" in after_brand["say_to_user"]
    assert "three APIs" in after_brand["say_to_user"]


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


def test_owner_keeps_pasted_website_as_brand() -> None:
    record = ensure_buyer("bret-jenny")
    record["brand_name"] = "6Frame Studio"
    record["website_url"] = "https://coryconnects.tech/"
    record["brand_voice"] = (
        "Cinematic, precise, restrained. Write like a studio director, not a growth desk. "
        "Never use: game-changer, revolutionize, unlock, next-level, crush, viral hack. "
        "Prefer craft language: frame, cut, light, tempo, voice."
    )
    record = save_buyer(record)
    kept = apply_owner_studio_brand(record)
    assert kept["website_url"] == "https://coryconnects.tech/"
    assert kept["brand_name"] == "Cory Connects"
    assert "Write as Cory Connects" in (kept.get("brand_voice") or "")
    assert "brand of record" in (kept.get("brand_voice") or "")
    assert "Cinematic, precise, restrained" not in (kept.get("brand_voice") or "")


def test_owner_website_only_does_not_restore_six_frame() -> None:
    record = ensure_buyer("bret-jenny")
    record["brand_name"] = ""
    record["website_url"] = "https://www.coryconnects.tech"
    record["brand_voice"] = ""
    record = save_buyer(record)
    kept = apply_owner_studio_brand(load_buyer("bret-jenny") or record)
    assert "6framestudio" not in (kept.get("website_url") or "").lower()
    assert kept["brand_name"] == "Cory Connects"


def test_brand_from_host_and_homepage_not_thin_meta() -> None:
    assert brand_name_from_host("https://coryconnects.tech/") == "Cory Connects"
    parsed = brand_from_html(CORY_HTML, "https://coryconnects.tech/")
    assert parsed["brand_name"] == "Cory Connects"
    assert parsed["source"] == "homepage"
    assert "website concept" not in parsed["brand_voice"]
    assert "public signal" in parsed["brand_voice"]
    assert "brand of record" in parsed["brand_voice"]
    assert "Do not write as 6Frame" in parsed["brand_voice"]


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
        "proof_link",
    }.issubset(names)
