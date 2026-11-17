"""Per-buyer Content Team state. Draft-only; never publishes."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import data_dir


def buyer_state_dir(buyer_id: str) -> Path:
    path = data_dir() / "buyers" / buyer_id / "content_team"
    path.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def use_buyer_state(buyer_id: str) -> Iterator[Path]:
    root = buyer_state_dir(buyer_id)
    previous = os.environ.get("CONTENT_TEAM_STATE_DIR")
    os.environ["CONTENT_TEAM_STATE_DIR"] = str(root)
    os.environ.setdefault("DISABLE_BACKGROUND_SCHEDULER", "true")
    try:
        yield root
    finally:
        if previous is None:
            os.environ.pop("CONTENT_TEAM_STATE_DIR", None)
        else:
            os.environ["CONTENT_TEAM_STATE_DIR"] = previous
