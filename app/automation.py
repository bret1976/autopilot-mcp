from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.config import DEFAULT_DAILY_TIMEZONE
from app.jobs import is_busy, spawn_job
from app.onboard import readiness
from app.store import (
    WEEKDAYS,
    days_from_record,
    hours_from_record,
    iter_buyer_records,
    load_buyer,
    parse_bool,
    save_buyer,
)

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


def slot_id(when: datetime) -> str:
    return f"{when.date().isoformat()}T{when.hour:02d}"


def is_due(record: dict[str, Any], now: datetime | None = None) -> bool:
    if not automation_enabled(record):
        return False
    if not readiness(record).get("ready"):
        return False
    here = local_now(record, now)
    if here.weekday() not in days_from_record(record):
        return False
    hours = hours_from_record(record)
    if here.hour not in hours:
        return False
    slot = slot_id(here)
    last_slot = str(record.get("last_automation_slot") or "")
    if last_slot == slot:
        # Growth V2: one retry of a failed slot (cap 2 attempts), same slot hour only.
        from app import growth_v2

        return growth_v2.slot_retry_due(str(record.get("buyer_id") or ""), slot)
    today = here.date().isoformat()
    if last_slot:
        return True
    # Older licenses only stored a date. Treat that as already fired for today.
    return str(record.get("last_automation_date") or "") != today


def mark_fired(buyer_id: str, when: datetime) -> dict[str, Any]:
    record = load_buyer(buyer_id)
    if not record:
        raise KeyError(buyer_id)
    record["last_automation_date"] = when.date().isoformat()
    record["last_automation_slot"] = slot_id(when)
    return save_buyer(record)


def describe_schedule(record: dict[str, Any]) -> str:
    hours = hours_from_record(record)
    days = days_from_record(record)
    tz = str(record.get("daily_run_timezone") or DEFAULT_DAILY_TIMEZONE)
    times = " and ".join(f"{hour:02d}:00" for hour in hours)
    if days == list(range(7)):
        day_bit = "every day"
    elif days == [0, 1, 2, 3, 4]:
        day_bit = "weekdays"
    elif days == [5, 6]:
        day_bit = "weekends"
    else:
        day_bit = ", ".join(WEEKDAYS[i] for i in days)
    return f"{day_bit} at {times} {tz}"


def due_buyers(now: datetime | None = None) -> list[dict[str, Any]]:
    return [record for record in iter_buyer_records() if is_due(record, now)]


def automation_public(record: dict[str, Any]) -> dict[str, Any]:
    here = local_now(record)
    enabled = automation_enabled(record)
    approval = require_approval(record)
    hours = hours_from_record(record)
    days = days_from_record(record)
    tz = str(record.get("daily_run_timezone") or DEFAULT_DAILY_TIMEZONE)
    pending = bool((record.get("last_run") or {}).get("pending_approval"))
    schedule = describe_schedule(record)
    return {
        "automation_enabled": enabled,
        "require_approval": approval,
        "daily_run_hour": hours[0],
        "daily_run_hours": hours,
        "daily_run_days": [WEEKDAYS[i] for i in days],
        "daily_run_timezone": tz,
        "last_automation_date": record.get("last_automation_date") or None,
        "last_automation_slot": record.get("last_automation_slot") or None,
        "pending_approval": pending,
        "next_local": schedule,
        "schedule": schedule,
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
    from app import growth_v2

    slot = slot_id(here)
    if growth_v2.enabled():
        row = growth_v2.slot_attempts(buyer_id, slot)
        if int(row.get("attempts") or 0) >= growth_v2.max_slot_attempts():
            mark_fired(buyer_id, here)
            return None
        growth_v2.note_slot_attempt(buyer_id, slot)
    mark_fired(buyer_id, here)
    draft = require_approval(record)

    async def _job() -> dict[str, Any]:
        from app.mcp_server import bind_buyer
        from app.spine import run_autopilot

        bind_buyer(buyer_id)
        fresh = load_buyer(buyer_id) or record
        ok = False
        try:
            result = await run_autopilot(
                fresh,
                mock=False,
                draft=draft,
                via="automation",
            )
            posts = ((result or {}).get("publish") or {}).get("posts") or []
            ok = bool(result.get("ok")) and (draft or any(p.get("ok") for p in posts))
            return result
        finally:
            if growth_v2.enabled():
                growth_v2.note_slot_attempt(buyer_id, slot, ok=ok)
                if not ok:
                    growth_v2.add_alert(buyer_id, "slot_failed", f"Automation slot {slot} produced no live post.")

    return spawn_job(
        buyer_id,
        "automation",
        _job,
        {"via": "automation", "draft": draft, "require_approval": draft},
    )


_last_deletion_check = 0.0


async def _deletion_sweep() -> None:
    global _last_deletion_check
    import time as _time

    from app import growth_v2

    if not growth_v2.enabled() or _time.time() - _last_deletion_check < 6 * 3600:
        return
    _last_deletion_check = _time.time()
    for record in iter_buyer_records():
        try:
            await growth_v2.check_deletions(record)
        except Exception:  # noqa: BLE001
            logger.exception("deletion check failed for %s", record.get("buyer_id"))
        try:
            # Per-post likes/comments/shares + follower counts (every 6 hours, same sweep).
            await growth_v2.refresh_metrics(record)
        except Exception:  # noqa: BLE001
            logger.exception("metrics refresh failed for %s", record.get("buyer_id"))


async def tick(now: datetime | None = None) -> list[str]:
    started: list[str] = []
    if now is None:
        try:
            await _deletion_sweep()
        except Exception:  # noqa: BLE001
            logger.exception("deletion sweep failed")
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
