from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ["DATA_DIR"] = "/tmp/autopilot-mcp-run-guard-tests"
os.environ["RUN_GUARD_BLOCK"] = "1"
os.environ["RUN_GUARD_WINDOW_SEC"] = "3600"

import pytest

from app import run_guard


@pytest.fixture(autouse=True)
def _clean_ledger(tmp_path, monkeypatch):
    ledger = tmp_path / "run_guard_ledger.json"
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RUN_GUARD_LEDGER", str(ledger))
    monkeypatch.setenv("RUN_GUARD_BLOCK", "1")
    if ledger.exists():
        ledger.unlink()
    yield
    if ledger.exists():
        ledger.unlink()


def _copy():
    return {
        "title": "Morning cut",
        "captions": {
            "facebook": "Fresh morning cut for the brand #Topic",
            "twitter": "Fresh morning cut for the brand #Topic",
        },
    }


def _media():
    return {
        "vertical_url": "https://cdn.example/v1/clip-a.mp4",
        "landscape_url": "https://cdn.example/v1/clip-a-wide.mp4",
    }


def test_fingerprint_stable():
    a = run_guard.content_fingerprint(
        buyer_id="studio-alpha",
        platforms=["facebook", "twitter"],
        copy=_copy(),
        media=_media(),
    )
    b = run_guard.content_fingerprint(
        buyer_id="studio-alpha",
        platforms=["twitter", "facebook"],
        copy=_copy(),
        media=_media(),
    )
    assert a and a == b


def test_blocks_duplicate_after_record():
    check = run_guard.check_publish(
        buyer_id="studio-alpha",
        platforms=["facebook", "twitter"],
        copy=_copy(),
        media=_media(),
    )
    assert check["reason"] == "ok"
    assert check["blocked"] is False

    rec = run_guard.record_publish(
        buyer_id="studio-alpha",
        platforms=["facebook", "twitter"],
        copy=_copy(),
        media=_media(),
    )
    assert rec["recorded"] is True

    again = run_guard.check_publish(
        buyer_id="studio-alpha",
        platforms=["facebook", "twitter"],
        copy=_copy(),
        media=_media(),
    )
    assert again["blocked"] is True
    assert again["reason"] == "duplicate_fingerprint"

    # Different buyer is fine
    other = run_guard.check_publish(
        buyer_id="studio-beta",
        platforms=["facebook", "twitter"],
        copy=_copy(),
        media=_media(),
    )
    assert other["blocked"] is False
    assert other["reason"] == "ok"


def test_force_overrides_block():
    run_guard.record_publish(
        buyer_id="studio-alpha",
        platforms=["facebook"],
        copy=_copy(),
        media=_media(),
    )
    forced = run_guard.check_publish(
        buyer_id="studio-alpha",
        platforms=["facebook"],
        copy=_copy(),
        media=_media(),
        force=True,
    )
    assert forced["blocked"] is False
    assert forced["reason"] == "forced"


def test_summary_pack_marker():
    s = run_guard.summary()
    assert s["pack"] == "run-guard-v1"
    assert s["blocking"] is True
    assert s["window_sec"] == 3600
