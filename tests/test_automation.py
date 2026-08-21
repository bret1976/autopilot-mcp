from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-automation-tests")

import pytest

from app.automation import (
    automation_public,
    due_buyers,
    is_due,
    mark_fired,
    require_approval,
    tick,
)
from app.mcp_server import bind_buyer, mcp, set_automation
from app.onboard import readiness
from app.store import ensure_buyer, parse_bool, public_config, update_setup


PT = ZoneInfo("America/Los_Angeles")


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))


def _ready(buyer_id: str) -> dict:
    ensure_buyer(buyer_id)
    return update_setup(
        buyer_id,
        {
            "gemini_api_key": "test-gemini-not-real",
            "postproxy_api_key": "test-postproxy-not-real",
            "postproxy_profile_group_id": "grp_test",
            "brand_name": "North Light",
            "website_url": "https://northlight.example",
            "brand_voice": "Write as North Light.",
            "daily_run_hour": 8,
            "daily_run_timezone": "America/Los_Angeles",
        },
    )


def test_parse_bool() -> None:
    assert parse_bool("false", True) is False
    assert parse_bool("true", False) is True
    assert parse_bool("off", True) is False
    assert parse_bool(None, True) is True


def test_due_only_when_enabled_ready_and_hour() -> None:
    record = _ready("auto-due")
    eight = datetime(2026, 8, 21, 8, 5, tzinfo=PT)
    nine = datetime(2026, 8, 21, 9, 5, tzinfo=PT)
    assert is_due(record, eight) is False
    enabled = update_setup("auto-due", {"automation_enabled": True, "require_approval": False})
    assert is_due(enabled, eight) is True
    assert is_due(enabled, nine) is False
    mark_fired("auto-due", eight)
    again = ensure_buyer("auto-due")
    assert is_due(again, eight) is False


def test_require_approval_defaults_on() -> None:
    record = _ready("auto-approve-default")
    assert require_approval(record) is True
    off = update_setup("auto-approve-default", {"require_approval": False})
    assert require_approval(off) is False
    pub = public_config(off)
    assert pub["automation_enabled"] is False
    assert pub["require_approval"] is False


def test_due_buyers_skips_unready() -> None:
    ensure_buyer("auto-empty")
    update_setup("auto-empty", {"automation_enabled": True})
    ready = _ready("auto-ready")
    update_setup("auto-ready", {"automation_enabled": True})
    eight = datetime(2026, 8, 21, 8, 1, tzinfo=PT)
    ids = {row["buyer_id"] for row in due_buyers(eight)}
    assert "auto-empty" not in ids
    assert "auto-ready" in ids


@pytest.mark.asyncio
async def test_set_automation_saves_mode() -> None:
    bind_buyer("auto-setup")
    _ready("auto-setup")
    report = await set_automation(
        enabled=True,
        require_approval=False,
        daily_run_hour=9,
        daily_run_timezone="America/Los_Angeles",
    )
    assert report["ok"] is True
    assert report["automation"]["automation_enabled"] is True
    assert report["automation"]["require_approval"] is False
    assert report["automation"]["mode"] == "scan_and_post"
    assert "no approval" in report["message"].lower() or "no click" in report["say_to_user"].lower()

    gated = await set_automation(enabled=True, require_approval=True)
    assert gated["automation"]["mode"] == "approve_then_post"


@pytest.mark.asyncio
async def test_set_automation_refuses_until_ready() -> None:
    bind_buyer("auto-blocked")
    ensure_buyer("auto-blocked")
    refused = await set_automation(enabled=True, require_approval=False)
    assert refused.get("needs_setup") is True
    assert refused.get("ok") is False


@pytest.mark.asyncio
async def test_tick_starts_job_with_approval_flag(monkeypatch) -> None:
    buyer = _ready("auto-tick")
    update_setup("auto-tick", {"automation_enabled": True, "require_approval": True})
    seen: dict[str, object] = {}

    async def fake_run(record, **kwargs):
        seen.update(kwargs)
        seen["buyer_id"] = record["buyer_id"]
        return {"ok": True, "draft": kwargs.get("draft"), "via": kwargs.get("via")}

    monkeypatch.setattr("app.spine.run_autopilot", fake_run)
    eight = datetime(2026, 8, 21, 8, 2, tzinfo=PT)
    started = await tick(eight)
    assert "auto-tick" in started
    await asyncio_wait()
    assert seen.get("via") == "automation"
    assert seen.get("draft") is True
    assert is_due(ensure_buyer("auto-tick"), eight) is False
    assert buyer["buyer_id"] == "auto-tick"


async def asyncio_wait() -> None:
    import asyncio

    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_tick_auto_posts_when_approval_off(monkeypatch) -> None:
    _ready("auto-hot")
    update_setup("auto-hot", {"automation_enabled": True, "require_approval": False})
    seen: dict[str, object] = {}

    async def fake_run(record, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr("app.spine.run_autopilot", fake_run)
    eight = datetime(2026, 8, 21, 8, 3, tzinfo=PT)
    started = await tick(eight)
    assert "auto-hot" in started
    await asyncio_wait()
    assert seen.get("draft") is False


@pytest.mark.asyncio
async def test_automation_tools_registered() -> None:
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert {"set_automation", "approve_and_publish", "run_autopilot"}.issubset(names)


def test_ready_copy_mentions_automation_choice() -> None:
    record = _ready("auto-copy")
    report = readiness(record)
    assert "set_automation" in report["say_to_user"]
    assert "require_approval" in report["say_to_user"]
    pub = automation_public(record)
    assert pub["mode"] == "off"
