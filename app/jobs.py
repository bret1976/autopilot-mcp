from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

_running: set[str] = set()

BUSY = {
    "ok": False,
    "busy": True,
    "say_to_user": (
        "A Gemini job is already running for this license. "
        "Do not fire scan_trends and run_autopilot at the same time. "
        "Wait for the first tool to finish, then call one of them."
    ),
}


def is_busy(buyer_id: str) -> bool:
    return buyer_id in _running


@asynccontextmanager
async def buyer_job(buyer_id: str) -> AsyncIterator[dict[str, Any] | None]:
    if buyer_id in _running:
        yield BUSY
        return
    _running.add(buyer_id)
    try:
        yield None
    finally:
        _running.discard(buyer_id)
