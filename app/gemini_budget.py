"""Gemini Daily Budget Pack — cap paid Gemini generateContent calls per UTC day.

Buyers bring their own Gemini keys; automation + agent tools can still burn
prepaid credit if a run loops (rescan) or an agent hammers scan_trends /
write_copy. This pack counts every real HTTP attempt in gemini._call_model_raw
and soft-blocks further calls once a scope (buyer_id) or the global pool hits
its daily limit. Resets at 00:00 UTC.

Idea inspiration (patterns only, no code copied):
- ZhuoZhuoCrayon/throttled-py (MIT) — fixed/sliding window + token-bucket ideas
  for high-volume Python rate limits.
- laurentS/slowapi (MIT) — FastAPI-friendly per-key counters.
- Google AI Studio billing docs — prepaid credits debit per successful/attempted
  generateContent; apps should bound call volume themselves.
- olcayto-akbudak/retry-storm-simulator (MIT) — retry/budget storm awareness.

Original autopilot-mcp Python. Backend only: no UI, templates, or screens change.
Kill switch: GEMINI_DAILY_BUDGET=0
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import data_dir

PACK = "gemini-daily-budget-v1"
DEFAULT_PER_SCOPE = 48  # ~16–24 autopilot runs/day at 2–3 calls each
DEFAULT_GLOBAL = 2000
MAX_SCOPES = 2000

_lock = threading.RLock()
_counters = {
    "calls_allowed": 0,
    "calls_blocked": 0,
    "scopes_blocked": 0,
    "global_blocked": 0,
}


class BudgetExceeded(RuntimeError):
    """Raised when a Gemini call would exceed the daily budget."""

    def __init__(self, message: str, *, retry_after_sec: int, scope: str) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec
        self.scope = scope


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except ValueError:
        return default


def enabled() -> bool:
    raw = (os.environ.get("GEMINI_DAILY_BUDGET") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def per_scope_limit() -> int:
    return _int_env("GEMINI_DAILY_CALLS_PER_SCOPE", DEFAULT_PER_SCOPE)


def global_limit() -> int:
    return _int_env("GEMINI_DAILY_CALLS_GLOBAL", DEFAULT_GLOBAL)


def _ledger_path() -> Path:
    override = os.environ.get("GEMINI_DAILY_BUDGET_LEDGER")
    if override:
        return Path(override)
    return data_dir() / "gemini_daily_budget.json"


def _utc_day(now: float | None = None) -> str:
    ts = now if now is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _seconds_until_utc_midnight(now: float | None = None) -> int:
    ts = now if now is not None else time.time()
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    tomorrow = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc).timestamp() + 86400
    return max(1, int(tomorrow - ts + 0.999))


def _load() -> dict[str, Any]:
    path = _ledger_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("day", _utc_day())
            data.setdefault("global", 0)
            data.setdefault("scopes", {})
            return data
    except (OSError, ValueError):
        pass
    return {"day": _utc_day(), "global": 0, "scopes": {}}


def _save(data: dict[str, Any]) -> None:
    scopes = data.get("scopes") or {}
    if len(scopes) > MAX_SCOPES:
        # Drop lowest-count scopes first (keep hot buyers).
        ordered = sorted(scopes.items(), key=lambda kv: int(kv[1] or 0))
        data["scopes"] = dict(ordered[-MAX_SCOPES:])
    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _roll(data: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    day = _utc_day(now)
    if data.get("day") != day:
        return {"day": day, "global": 0, "scopes": {}}
    return data


def normalize_scope(scope: str | None) -> str:
    value = (scope or "").strip().lower() or "anon"
    # Keep ledger keys short and path-safe.
    return value[:120]


def peek(scope: str | None = None, *, now: float | None = None) -> dict[str, Any]:
    """Non-mutating view of remaining budget for a scope."""
    key = normalize_scope(scope)
    with _lock:
        data = _roll(_load(), now)
        used_scope = int((data.get("scopes") or {}).get(key) or 0)
        used_global = int(data.get("global") or 0)
    per = per_scope_limit()
    glob = global_limit()
    return {
        "pack": PACK,
        "enabled": enabled(),
        "day": _utc_day(now),
        "scope": key,
        "scope_used": used_scope,
        "scope_limit": per,
        "scope_remaining": max(0, per - used_scope),
        "global_used": used_global,
        "global_limit": glob,
        "global_remaining": max(0, glob - used_global),
        "retry_after_sec": _seconds_until_utc_midnight(now),
    }


def consume(scope: str | None = None, *, now: float | None = None) -> dict[str, Any]:
    """Record one Gemini HTTP attempt. Raises BudgetExceeded when over limit."""
    key = normalize_scope(scope)
    if not enabled():
        with _lock:
            _counters["calls_allowed"] += 1
        return peek(key, now=now)

    with _lock:
        data = _roll(_load(), now)
        scopes = data.setdefault("scopes", {})
        used_scope = int(scopes.get(key) or 0)
        used_global = int(data.get("global") or 0)
        per = per_scope_limit()
        glob = global_limit()
        retry = _seconds_until_utc_midnight(now)

        if used_global >= glob:
            _counters["calls_blocked"] += 1
            _counters["global_blocked"] += 1
            raise BudgetExceeded(
                f"Gemini daily global budget exhausted ({used_global}/{glob} UTC day "
                f"{data.get('day')}). Retry after ~{retry}s.",
                retry_after_sec=retry,
                scope=key,
            )
        if used_scope >= per:
            _counters["calls_blocked"] += 1
            _counters["scopes_blocked"] += 1
            raise BudgetExceeded(
                f"Gemini daily budget exhausted for scope '{key}' "
                f"({used_scope}/{per} UTC day {data.get('day')}). Retry after ~{retry}s.",
                retry_after_sec=retry,
                scope=key,
            )

        scopes[key] = used_scope + 1
        data["global"] = used_global + 1
        _save(data)
        _counters["calls_allowed"] += 1

    return peek(key, now=now)


def summary() -> dict[str, Any]:
    with _lock:
        data = _roll(_load())
        counters = dict(_counters)
        scopes = data.get("scopes") or {}
        top = sorted(
            ((k, int(v or 0)) for k, v in scopes.items()),
            key=lambda kv: kv[1],
            reverse=True,
        )[:10]
    return {
        "pack": PACK,
        "enabled": enabled(),
        "day": data.get("day"),
        "per_scope_limit": per_scope_limit(),
        "global_limit": global_limit(),
        "global_used": int(data.get("global") or 0),
        "scopes_tracked": len(scopes),
        "top_scopes": [{"scope": k, "calls": v} for k, v in top],
        "ledger_path": str(_ledger_path()),
        "retry_after_sec": _seconds_until_utc_midnight(),
        **counters,
    }


def reset_for_tests() -> None:
    with _lock:
        for key in _counters:
            _counters[key] = 0
        path = _ledger_path()
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
