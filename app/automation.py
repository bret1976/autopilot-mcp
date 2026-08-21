from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import DEFAULT_DAILY_HOUR, DEFAULT_DAILY_TIMEZONE
from app.jobs import is_busy, spawn_job
from app.onboard import readiness
from app.store import iter_buyer_records, load_buyer, parse_bool, save_buyer

logger = logging.getLogger("automation")

SCHEDULER_INTERVAL_SECONDS = 60
scheduler_started = False


def automation_enabled(record: dict[str, Any]) -> bool:
    return parse_bool(record.get("automation_enabled"), False)


def require_approval(record: dict[str, Any]) -> bool:
    return parse_bool(record.get("require_approval"), True)


def zone_for(record: dict[str, Any]) -> ZoneInfo:
    name = str(record.get("daily_run_timezone") or DEFAULT_DAILY_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo(DEFAULT_DAILY_TIMEZONE)


def local_now(record: dict[str, Any], now: datetime | None = None) -> datetime:
    tz = zone_for(record)
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def is_due(record: dict[str, Any], now: datetime | None = None) -> bool:
    if not automation_enabled(record):
        return False
    if not readiness(record).get("ready"):
        return False
    here = local_now(record, now)
    try:
        hour = int(record.get("daily_run_hour") if record.get("daily_run_hour") is not None else DEFAULT_DAILY_HOUR)
    except (TypeError, ValueError):
        hour = DEFAULT_DAILY_HOUR
    hour = max(0, min(hour, 23))
    if here.hour != hour:
        return False
    today = here.date().isoformat()
    return str(record.get("last_automation_date") or "") != today


def mark_fired(buyer_id: str, when: datetime) -> dict[str, Any]:
    record = load_buyer(buyer_id)
    if not record:
        raise KeyError(buyer_id)
    record["last_automation_date"] = when.date().isoformat()
    return save_buyer(record)


def due_buyers(now: datetime | None = None) -> list[dict[str, Any]]:
    return [record for record in iter_buyer_records() if is_due(record, now)]


def automation_public(record: dict[str, Any]) -> dict[str, Any]:
    here = local_now(record)
    enabled = automation_enabled(record)
    approval = require_approval(record)
    hour = record.get("daily_run_hour")
    try:
        hour = int(hour if hour is not None else DEFAULT_DAILY_HOUR)
    except (TypeError, ValueError):
        hour = DEFAULT_DAILY_HOUR
    tz = str(record.get("daily_run_timezone") or DEFAULT_DAILY_TIMEZONE)
    pending = bool((record.get("last_run") or {}).get("pending_approval"))
    return {
        "automation_enabled": enabled,
        "require_approval": approval,
        "daily_run_hour": max(0, min(hour, 23)),
        "daily_run_timezone": tz,
        "last_automation_date": record.get("last_automation_date") or None,
        "pending_approval": pending,
        "next_local": f"{hour:02d}:00 {tz}",
        "mode": (
            "off"
            if not enabled
            else ("approve_then_post" if approval else "scan_and_post")
        ),
        "local_now": here.isoformat(),
    }


async def run_due_buyer(record: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    buyer_id = str(record.get("buyer_id") or "")
    if not buyer_id or is_busy(buyer_id) or not is_due(record, now):
        return None
    here = local_now(record, now)
    mark_fired(buyer_id, here)
    draft = require_approval(record)

    async def _job() -> dict[str, Any]:
        from app.mcp_server import bind_buyer
        from app.spine import run_autopilot

        bind_buyer(buyer_id)
        fresh = load_buyer(buyer_id) or record
        return await run_autopilot(
            fresh,
            mock=False,
            draft=draft,
            via="automation",
        )

    return spawn_job(
        buyer_id,
        "automation",
        _job,
        {"via": "automation", "draft": draft, "require_approval": draft},
    )


async def tick(now: datetime | None = None) -> list[str]:
    started: list[str] = []
    for record in due_buyers(now):
        try:
            job = await run_due_buyer(record, now)
        except Exception:  # noqa: BLE001
            logger.exception("automation tick failed for %s", record.get("buyer_id"))
            continue
        if job:
            started.append(str(record.get("buyer_id")))
    return started


async def scheduler_loop(stop: asyncio.Event | None = None) -> None:
    global scheduler_started
    scheduler_started = True
    logger.info("Buyer automation scheduler started")
    while stop is None or not stop.is_set():
        try:
            await tick()
        except Exception:  # noqa: BLE001
            logger.exception("automation scheduler tick failed")
        if stop is None:
            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
            continue
        try:
            await asyncio.wait_for(stop.wait(), timeout=SCHEDULER_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue
