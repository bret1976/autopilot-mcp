from __future__ import annotations

import json
import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-tests")

import pytest

from app.mcp_server import bind_buyer, mcp, onboard, run_autopilot_tool, setup
from app.onboard import brand_from_html, brand_name_from_host, hashtags_from_name, readiness
from app.store import apply_owner_studio_brand, ensure_buyer, load_buyer, reset_instance, save_buyer

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
    reset_instance("fresh-studio")
    report = await onboard()
    assert report["needs_setup"] is True
    assert report["ready"] is False
    assert report["missing"] == ["website_url"]
    assert "website" in report["say_to_user"].lower()
    assert report["step_name"] == "ask_website"
    assert "loaded and ready" in report["say_to_user"].lower()
    assert "6frame" not in report["say_to_user"].lower()
    assert report["config"]["brand_name"] is None
    assert report["config"]["website_url"] is None

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
    reset_instance("brand-first")
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
    assert "Branding for Cory Connects" in after_brand["say_to_user"]
    assert "now confirmed" in after_brand["say_to_user"]
    assert "three APIs" in after_brand["say_to_user"]
    assert after_brand["step_name"] == "ask_apis"

    after_keys = await setup(
        gemini_api_key="test-gemini-not-real",
        postproxy_api_key="test-postproxy-not-real",
        postproxy_profile_group_id="grp_test",
    )
    assert after_keys["ready"] is True
    assert after_keys["step_name"] == "ask_schedule"
    assert "twice a day" in after_keys["say_to_user"].lower()
    assert "set_automation" in after_keys["say_to_user"]
    assert "run_autopilot" not in after_keys["say_to_user"]
    assert "Do not post yet" in after_keys["say_to_user"]


@pytest.mark.asyncio
async def test_onboard_wipes_leftover_company_every_time() -> None:
    bind_buyer("bret-jenny")
    record = ensure_buyer("bret-jenny")
    record.update(
        {
            "brand_name": "Secured Quantum Services",
            "brand_voice": "SQS leftover voice",
            "website_url": "https://securedquantum.example",
            "gemini_api_key": "old-gemini",
            "postproxy_api_key": "old-pp",
            "postproxy_profile_group_id": "old-group",
            "last_run": {"id": "old-run", "posts": [{"account": "6Frame Studios"}]},
            "automation_enabled": True,
            "require_approval": True,
            "daily_run_hour": 8,
        }
    )
    save_buyer(record)

    result = await onboard()

    assert result["ready"] is False
    assert result["needs_setup"] is True
    assert result["missing"] == ["website_url"]
    assert "website" in result["say_to_user"].lower()
    assert "Secured Quantum" not in result["say_to_user"]
    assert "TrendPilot" in result["say_to_user"]
    assert "6frame" not in result["say_to_user"].lower()
    assert "6framestudio" not in json.dumps(result).lower()
    wiped = load_buyer("bret-jenny") or {}
    assert wiped.get("brand_name") == ""
    assert wiped.get("gemini_api_key") == ""
    assert wiped.get("last_run") is None
    assert wiped.get("automation_enabled") is False


@pytest.mark.asyncio
async def test_onboard_never_mentions_six_frame_leftover() -> None:
    bind_buyer("bret-jenny")
    record = ensure_buyer("bret-jenny")
    record.update(
        {
            "brand_name": "6Frame Studio",
            "brand_voice": "Cinematic leftover studio voice",
            "website_url": "https://6framestudio.com/",
            "gemini_api_key": "old-gemini",
            "postproxy_api_key": "old-pp",
            "postproxy_profile_group_id": "old-group",
            "last_run": {"id": "old-6frame"},
            "automation_enabled": True,
        }
    )
    save_buyer(record)

    result = await onboard()
    blob = json.dumps(result).lower()
    assert result["missing"] == ["website_url"]
    assert result["step_name"] == "ask_website"
    assert "loaded and ready" in result["say_to_user"].lower()
    assert "paste your company website" in result["say_to_user"].lower()
    assert "6frame" not in blob
    assert "6framestudio" not in blob
    assert result["config"]["brand_name"] is None
    assert result["config"]["website_url"] is None
    wiped = load_buyer("bret-jenny") or {}
    assert wiped.get("brand_name") == ""
    assert wiped.get("website_url") == ""


@pytest.mark.asyncio
async def test_new_website_resets_previous_company_keys() -> None:
    bind_buyer("switch-brand")
    ensure_buyer("switch-brand")
    await setup(
        gemini_api_key="old-gemini",
        postproxy_api_key="old-pp",
        postproxy_profile_group_id="old-group",
        brand_name="Cory Connects",
        website_url="https://coryconnects.example",
        brand_voice="Write as Cory Connects.",
    )
    switched = await setup(
        brand_name="North Light",
        website_url="https://northlight.example",
        brand_voice="Write as North Light.",
    )
    assert switched["ready"] is False
    assert switched["config"]["brand_name"] == "North Light"
    assert switched["missing"] == [
        "gemini_api_key",
        "postproxy_api_key",
        "postproxy_profile_group_id",
    ]
    assert "Branding for North Light" in switched["say_to_user"]
    assert "now confirmed" in switched["say_to_user"]


def test_readiness_and_hashtag_from_name() -> None:
    empty = readiness({"gemini_api_key": "", "postproxy_api_key": ""})
    assert empty["ready"] is False
    assert hashtags_from_name("North Light") == ["#NorthLight"]


def test_owner_license_clears_ian_leftover() -> None:
    record = ensure_buyer("bret-jenny")
    record["brand_name"] = "IAN Group"
    record["website_url"] = "https://iangroup.ai/"
    record = save_buyer(record)
    restored = apply_owner_studio_brand(record)
    assert restored["brand_name"] == ""
    assert restored["website_url"] == ""


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
async def test_onboard_with_website_locks_brand_and_asks_apis(monkeypatch) -> None:
    bind_buyer("site-first")
    ensure_buyer("site-first")

    async def fake_fetch(url: str) -> dict:
        return {
            "ok": True,
            "website_url": "https://www.sqs.world/",
            "brand_name": "Secured Quantum Services",
            "brand_voice": "Write as Secured Quantum Services.",
            "brand_hashtags": ["#SecuredQuantumServices"],
        }

    monkeypatch.setattr("app.mcp_server.fetch_brand_from_website", fake_fetch)
    result = await onboard(website_url="https://www.sqs.world/")
    assert result["ready"] is False
    assert result["config"]["brand_name"] == "Secured Quantum Services"
    assert result["missing"] == [
        "gemini_api_key",
        "postproxy_api_key",
        "postproxy_profile_group_id",
    ]
    assert "Branding for Secured Quantum Services" in (result.get("message") or result["say_to_user"])
    assert "now confirmed" in (result.get("message") or result["say_to_user"])
    assert "gemini" in result["say_to_user"].lower()
    assert result["step_name"] == "ask_apis"


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
        "confirm_schedule",
        "choose_start",
        "approve_and_publish",
        "proof_link",
    }.issubset(names)
