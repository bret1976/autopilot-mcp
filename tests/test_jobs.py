from __future__ import annotations

import asyncio
import os
import time

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-job-tests")

import pytest

from app.jobs import is_busy, load_job, spawn_job


@pytest.mark.asyncio
async def test_spawn_job_returns_immediately_then_finishes() -> None:
    async def slow() -> dict:
        await asyncio.sleep(0.05)
        return {"ok": True, "n": 7}

    started = time.time()
    payload = spawn_job("job-studio", "scan_trends", slow)
    assert time.time() - started < 0.05
    assert payload["started"] is True
    assert payload["job"]["status"] == "running"
    assert is_busy("job-studio") is True
    for _ in range(40):
        await asyncio.sleep(0.05)
        job = load_job("job-studio")
        if job and job.get("status") != "running":
            break
    assert job["status"] == "ok"
    assert job["result"]["n"] == 7
    assert is_busy("job-studio") is False
