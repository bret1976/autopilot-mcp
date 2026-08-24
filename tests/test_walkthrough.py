from __future__ import annotations

import json
import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-walkthrough-tests")

import pytest

from app.mcp_server import bind_buyer, choose_start, confirm_schedule, onboard, set_automation, setup
from app.store import ensure_buyer, load_buyer, normalize_hours, parse_clock_hour, reset_instance
from app.walkthrough import START_BOTH, START_NOW, START_SCHEDULED, hours_for_times_per_day, normalize_start_mode


@pytest.mark.asyncio
async def test_eight_step_clean_setup(monkeypatch) -> None:
    bind_buyer("clean-buyer")
    ensure_buyer("clean-buyer")

    step1 = await onboard()
    assert step1["step_name"] == "ask_website"
    assert "Step 1" in step1["say_to_user"]
    assert "Step 2" in step1["say_to_user"]
    assert "6frame" not in json.dumps(step1).lower()

    async def fake_fetch(url: str) -> dict:
        return {
            "ok": True,
            "website_url": "https://northlight.example",
            "brand_name": "North Light",
            "brand_voice": "Write as North Light.",
            "brand_hashtags": ["#NorthLight"],
        }

    monkeypatch.setattr("app.mcp_server.fetch_brand_from_website", fake_fetch)
    step3 = await onboard(website_url="https://northlight.example")
    assert step3["step_name"] == "ask_apis"
    assert "Branding for North Light" in step3["say_to_user"]
    assert "now confirmed" in step3["say_to_user"]
    assert "Step 4" in step3["say_to_user"]
    assert "Cloud Browser" in step3["say_to_user"]

    step5 = await setup(
        gemini_api_key="test-gemini-not-real",
        postproxy_api_key="test-postproxy-not-real",
        postproxy_profile_group_id="grp_test",
    )
    assert step5["ready"] is True
    assert step5["step_name"] == "ask_schedule"
    assert "twice a day" in step5["say_to_user"].lower()
    assert "Do not post yet" in step5["say_to_user"]

    draft = await set_automation(times="8:00 AM and 5:00 PM", times_per_day=2)
    assert draft["step_name"] == "confirm_schedule"
    assert draft["automation"]["automation_enabled"] is False
    assert draft["choices"][0]["id"] == "confirm_schedule"
    assert "Confirm schedule" in draft["say_to_user"]

    locked = await confirm_schedule()
    assert locked["step_name"] == "choose_start"
    assert locked["automation"]["automation_enabled"] is True
    assert locked["automation"]["daily_run_hours"] == [8, 17]
    labels = [item["label"] for item in locked["choices"]]
    assert "Autopost Right Now" in labels
    assert "Start AutoPost at scheduled times" in labels
    assert any("right now and then start automation" in item.lower() for item in labels)

    waited = await choose_start("scheduled")
    assert waited["ok"] is True
    assert waited["start_mode"] == START_SCHEDULED
    assert waited["step_name"] == "running"
    assert "wait" in waited["say_to_user"].lower()
    saved = load_buyer("clean-buyer") or {}
    assert saved.get("automation_enabled") is True
    assert saved.get("start_mode") == START_SCHEDULED


@pytest.mark.asyncio
async def test_choose_start_now_runs_autopilot(monkeypatch) -> None:
    bind_buyer("now-buyer")
    ensure_buyer("now-buyer")
    reset_instance("now-buyer")
    await setup(
        gemini_api_key="test-gemini-not-real",
        postproxy_api_key="test-postproxy-not-real",
        postproxy_profile_group_id="grp_test",
        brand_name="North Light",
        website_url="https://northlight.example",
        brand_voice="Write as North Light.",
    )
    await set_automation(times="8am and 5pm")
    await confirm_schedule()

    seen: dict[str, object] = {}

    async def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "draft": kwargs.get("draft"), "started": True}

    monkeypatch.setattr("app.mcp_server.run_autopilot_tool", fake_run)
    result = await choose_start("now_and_scheduled")
    assert result["start_mode"] == START_BOTH
    assert result.get("draft") is False or seen.get("draft") is False


@pytest.mark.asyncio
async def test_cached_host_can_start_via_set_automation(monkeypatch) -> None:
    bind_buyer("cached-host")
    ensure_buyer("cached-host")
    reset_instance("cached-host")
    await setup(
        gemini_api_key="test-gemini-not-real",
        postproxy_api_key="test-postproxy-not-real",
        postproxy_profile_group_id="grp_test",
        brand_name="North Light",
        website_url="https://northlight.example",
        brand_voice="Write as North Light.",
    )
    seen: dict[str, object] = {}

    async def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return {"ok": True, "draft": kwargs.get("draft"), "started": True}

    monkeypatch.setattr("app.mcp_server.run_autopilot_tool", fake_run)
    result = await set_automation(times="8:00 AM and 5:00 PM", confirm=True, start_mode="now")
    assert result["start_mode"] == START_NOW
    assert seen.get("draft") is False


def test_clock_and_start_helpers() -> None:
    assert parse_clock_hour("5pm") == 17
    assert parse_clock_hour("5:00 PM") == 17
    assert parse_clock_hour("8:00 AM") == 8
    assert parse_clock_hour("12am") == 0
    assert parse_clock_hour("12pm") == 12
    assert normalize_hours("8:00 AM and 5:00 PM") == [8, 17]
    assert hours_for_times_per_day(2) == [8, 17]
    assert normalize_start_mode("Autopost Right Now") == START_NOW
    assert normalize_start_mode("Start AutoPost at scheduled times") == START_SCHEDULED
    assert normalize_start_mode("Autopost right now and then start automation for scheduled times") == START_BOTH
