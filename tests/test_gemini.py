from __future__ import annotations

import asyncio
import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-gemini-tests")

import httpx
import pytest

from app.config import GEMINI_COPY_MODELS, GEMINI_SCAN_MODELS, GEMINI_TIMEOUT_SECONDS
from app.gemini import generate_text
from app.jobs import load_job, save_job, spawn_job
from app.mcp_server import bind_buyer, run_autopilot_tool, scan_trends_tool
from app.store import ensure_buyer, save_buyer


def test_scan_uses_flash_only_copy_keeps_pro() -> None:
    assert GEMINI_SCAN_MODELS == ("gemini-3.6-flash",)
    assert GEMINI_COPY_MODELS[0] == "gemini-3.1-pro-preview"
    assert "gemini-3.6-flash" in GEMINI_COPY_MODELS
    assert GEMINI_TIMEOUT_SECONDS <= 25


@pytest.mark.asyncio
async def test_generate_text_fails_over_after_short_timeout(monkeypatch) -> None:
    seen: list[str] = []

    async def fake_call(api_key, model, prompt, *, grounded, timeout):
        seen.append(model)
        assert timeout <= 25
        if model == "gemini-3.1-pro-preview":
            raise RuntimeError("timeout after 22s")
        return '{"ok": true}'

    monkeypatch.setattr("app.gemini._call_model", fake_call)
    text = await generate_text("key", "hi", models=GEMINI_COPY_MODELS, timeout=22)
    assert text == '{"ok": true}'
    assert seen == ["gemini-3.1-pro-preview", "gemini-3.6-flash"]


@pytest.mark.asyncio
async def test_timeout_exception_is_labeled(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.TimeoutException("slow")

    monkeypatch.setattr("app.gemini.httpx.AsyncClient", FakeClient)
    with pytest.raises(RuntimeError, match="timeout after 22s"):
        await generate_text("key", "hi", models=("gemini-3.6-flash",), timeout=22)


@pytest.mark.asyncio
async def test_busy_lock_rejects_stacked_scan_and_run() -> None:
    bind_buyer("busy-studio")
    ensure_buyer("busy-studio")
    record = save_buyer(
        {
            **ensure_buyer("busy-studio"),
            "gemini_api_key": "test-gemini-not-real",
            "postproxy_api_key": "test-postproxy-not-real",
            "postproxy_profile_group_id": "grp",
            "brand_name": "KANEK",
            "website_url": "https://kanek.test",
        }
    )
    assert record["buyer_id"] == "busy-studio"

    async def hang() -> dict:
        await asyncio.sleep(30)
        return {"ok": True}

    first = spawn_job("busy-studio", "scan_trends", hang)
    assert first["started"] is True
    stacked = await scan_trends_tool()
    assert stacked["busy"] is True
    assert stacked["started"] is False
    run = await run_autopilot_tool()
    assert run["busy"] is True
    job = load_job("busy-studio")
    assert job is not None
    job["status"] = "ok"
    save_job(job)
