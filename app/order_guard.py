"""Order Guard Pack — paid-order idempotency + admin brute-force lockout + checkout throttle.

Idea inspiration (patterns only, no code copied):
- Stripe docs "Handle duplicate events" — record processed event ids and make
  fulfillment idempotent per Checkout Session.
- hookdeck/webhook-skills (MIT, Show HN) — webhook idempotency best practices.
- jazzband/django-axes (MIT) and AdamPflug/express-brute (MIT) — per-client
  failed-login counters with a temporary lockout window.
- laurentS/slowapi (MIT) — per-client sliding-window limits for FastAPI routes.

Original autopilot-mcp Python. Backend only: no UI, templates, or screens change.
- One Checkout Session -> one order row, no matter how many times an agent polls
  /api/orders/status or Stripe retries the webhook (same MCP URL is returned).
- Stripe webhook event ids are deduped (retries ack 200 with duplicate=true).
- Wrong admin secrets are counted per client IP; after ADMIN_LOCK_MAX failures in
  ADMIN_LOCK_WINDOW_SEC the client gets 429 until the window passes. A global cap
  also trips if many IPs guess at once.
- Public Checkout Session creation is limited per client IP (ORDER_CHECKOUT_PER_HOUR).
Kill switch: ORDER_GUARD=0.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from app.config import data_dir

PACK = "order-guard-v1"
MAX_SESSIONS = 2000
MAX_EVENTS = 2000

_lock = threading.RLock()
_admin_fail: dict[str, deque[float]] = {}
_admin_fail_global: deque[float] = deque()
_checkout_hits: dict[str, deque[float]] = {}
_status_hits: dict[str, deque[float]] = {}
_counters = {
    "duplicate_fulfillments_suppressed": 0,
    "duplicate_webhook_events": 0,
    "admin_failures": 0,
    "admin_lockouts": 0,
    "checkout_rate_limited": 0,
    "status_rate_limited": 0,
}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name) or default))
    except ValueError:
        return default


def enabled() -> bool:
    raw = (os.environ.get("ORDER_GUARD") or "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def admin_lock_max() -> int:
    return _int_env("ADMIN_LOCK_MAX", 8)


def admin_lock_window() -> int:
    return _int_env("ADMIN_LOCK_WINDOW_SEC", 15 * 60, 60)


def admin_lock_global_max() -> int:
    return _int_env("ADMIN_LOCK_GLOBAL_MAX", 60)


def checkout_per_hour() -> int:
    return _int_env("ORDER_CHECKOUT_PER_HOUR", 20)


def status_per_min() -> int:
    return _int_env("ORDER_STATUS_PER_MIN", 120)


# ---------------------------------------------------------------- ledger


def _ledger_path() -> Path:
    override = os.environ.get("ORDER_GUARD_LEDGER")
    if override:
        return Path(override)
    return data_dir() / "order_guard_ledger.json"


def _load() -> dict[str, Any]:
    path = _ledger_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("sessions", {})
            data.setdefault("events", {})
            return data
    except (OSError, ValueError):
        pass
    return {"sessions": {}, "events": {}}


def _save(data: dict[str, Any]) -> None:
    for key, cap in (("sessions", MAX_SESSIONS), ("events", MAX_EVENTS)):
        rows = data.get(key) or {}
        if len(rows) > cap:
            ordered = sorted(rows.items(), key=lambda kv: float((kv[1] or {}).get("at") or 0))
            data[key] = dict(ordered[-cap:])
    path = _ledger_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def lock() -> threading.RLock:
    return _lock


def prior_order(session_id: str | None) -> dict[str, Any] | None:
    """Order row already written for this Checkout Session, if any."""
    if not enabled() or not session_id:
        return None
    with _lock:
        row = _load()["sessions"].get(str(session_id))
    if isinstance(row, dict) and isinstance(row.get("order"), dict):
        with _lock:
            _counters["duplicate_fulfillments_suppressed"] += 1
        return dict(row["order"])
    return None


def remember_order(session_id: str | None, order: dict[str, Any]) -> None:
    if not enabled() or not session_id:
        return
    with _lock:
        data = _load()
        data["sessions"][str(session_id)] = {"at": time.time(), "order": order}
        _save(data)


def seen_event(event_id: str | None) -> bool:
    """True when this Stripe event id was already processed successfully."""
    if not enabled() or not event_id:
        return False
    with _lock:
        if str(event_id) in _load()["events"]:
            _counters["duplicate_webhook_events"] += 1
            return True
    return False


def mark_event(event_id: str | None) -> None:
    """Record a processed Stripe event id (call only after handling succeeded)."""
    if not enabled() or not event_id:
        return
    with _lock:
        data = _load()
        data["events"][str(event_id)] = {"at": time.time()}
        _save(data)


# ---------------------------------------------------------------- client limits


def client_ip(request: Any) -> str:
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client else None
    return str(host or "unknown")


def _prune(bucket: deque[float], window: float, now: float) -> None:
    while bucket and now - bucket[0] > window:
        bucket.popleft()


def admin_locked(ip: str) -> int:
    """Seconds until this client may try the admin secret again (0 = allowed)."""
    if not enabled():
        return 0
    now = time.time()
    window = admin_lock_window()
    with _lock:
        bucket = _admin_fail.get(ip)
        if bucket is not None:
            _prune(bucket, window, now)
            if len(bucket) >= admin_lock_max():
                return max(1, int(window - (now - bucket[0])))
        _prune(_admin_fail_global, window, now)
        if len(_admin_fail_global) >= admin_lock_global_max():
            return max(1, int(window - (now - _admin_fail_global[0])))
    return 0


def admin_failed(ip: str) -> None:
    if not enabled():
        return
    now = time.time()
    with _lock:
        bucket = _admin_fail.setdefault(ip, deque())
        bucket.append(now)
        _admin_fail_global.append(now)
        _counters["admin_failures"] += 1
        if len(bucket) == admin_lock_max():
            _counters["admin_lockouts"] += 1
        if len(_admin_fail) > 5000:
            _admin_fail.clear()


def admin_succeeded(ip: str) -> None:
    with _lock:
        _admin_fail.pop(ip, None)


def _hit(store: dict[str, deque[float]], ip: str, limit: int, window: float, counter: str) -> int:
    if not enabled():
        return 0
    now = time.time()
    with _lock:
        bucket = store.setdefault(ip, deque())
        _prune(bucket, window, now)
        if len(bucket) >= limit:
            _counters[counter] += 1
            return max(1, int(window - (now - bucket[0])))
        bucket.append(now)
        if len(store) > 5000:
            store.clear()
    return 0


def checkout_retry_after(ip: str) -> int:
    """0 when a new public Checkout Session may be created; else seconds to wait."""
    return _hit(_checkout_hits, ip, checkout_per_hour(), 3600, "checkout_rate_limited")


def status_retry_after(ip: str) -> int:
    return _hit(_status_hits, ip, status_per_min(), 60, "status_rate_limited")


def summary() -> dict[str, Any]:
    with _lock:
        data = _load()
        counters = dict(_counters)
    return {
        "pack": PACK,
        "enabled": enabled(),
        "sessions_fulfilled": len(data.get("sessions") or {}),
        "webhook_events_seen": len(data.get("events") or {}),
        "admin_lock_max": admin_lock_max(),
        "admin_lock_window_sec": admin_lock_window(),
        "checkout_per_hour": checkout_per_hour(),
        "status_per_min": status_per_min(),
        **counters,
    }


def reset_for_tests() -> None:
    with _lock:
        _admin_fail.clear()
        _admin_fail_global.clear()
        _checkout_hits.clear()
        _status_hits.clear()
        for key in _counters:
            _counters[key] = 0
