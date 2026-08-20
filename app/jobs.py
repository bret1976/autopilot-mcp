from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable

from app.config import data_dir

JobFn = Callable[[], Awaitable[dict[str, Any]]]

BUSY = {
    "ok": False,
    "busy": True,
    "started": False,
    "say_to_user": (
        "A job is already running for this license. Call status. "
        "Do not fire scan_trends and run_autopilot at the same time."
    ),
}


def _jobs_dir():
    path = data_dir() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _path(buyer_id: str):
    return _jobs_dir() / f"{buyer_id}.json"


def load_job(buyer_id: str) -> dict[str, Any] | None:
    path = _path(buyer_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_job(job: dict[str, Any]) -> dict[str, Any]:
    _path(str(job["buyer_id"])).write_text(json.dumps(job, indent=2), encoding="utf-8")
    return job


def is_busy(buyer_id: str) -> bool:
    job = load_job(buyer_id)
    return bool(job and job.get("status") == "running")


def started_payload(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "started": True,
        "busy": True,
        "poll": "status",
        "job": job,
        "say_to_user": (
            "Started in the background so Grok/Claude/Codex do not time out. "
            "Call status until job.status is ok or error. Do not start another run yet."
        ),
    }


def spawn_job(buyer_id: str, kind: str, factory: JobFn, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    current = load_job(buyer_id)
    if current and current.get("status") == "running":
        payload = dict(BUSY)
        payload["job"] = current
        return payload
    job = {
        "id": uuid.uuid4().hex[:12],
        "buyer_id": buyer_id,
        "kind": kind,
        "status": "running",
        "started_at": int(time.time()),
        "finished_at": None,
        "result": None,
        "error": None,
        "meta": meta or {},
    }
    save_job(job)

    async def _run() -> None:
        try:
            result = await factory()
            job.update(
                {
                    "status": "ok",
                    "finished_at": int(time.time()),
                    "result": result,
                }
            )
        except Exception as exc:  # noqa: BLE001
            job.update(
                {
                    "status": "error",
                    "finished_at": int(time.time()),
                    "error": str(exc),
                }
            )
        save_job(job)

    loop = asyncio.get_running_loop()
    loop.create_task(_run())
    return started_payload(job)
