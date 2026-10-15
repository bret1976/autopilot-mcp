from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")

import pytest
from fastapi.testclient import TestClient

from app import growth_v2 as g
from app.config import admin_secret


@pytest.fixture(autouse=True)
def _data(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("GROWTH_V2", raising=False)
    monkeypatch.delenv("GROWTH_V2_TRANSFORM", raising=False)


def test_youtube_title_is_hook_not_job_name():
    copy = {"title": "Autopilot: Latest scanned trend", "hook": "At DCRBN, this battery charges in 5 minutes flat and nobody noticed"}
    title = g.youtube_title_for(copy, "DCRBN")
    assert len(title) <= 60
    assert not title.lower().startswith(("at dcrbn", "autopilot"))
    assert "#shorts" not in title.lower()


def test_captions_rules():
    concept = {
        "hook": "DCRBN: This tiny reactor makes fuel from thin air https://x.co/abc",
        "lines": ["Carbon capture in a box. Learn more at dcrbn.com", "It runs on sunlight alone."],
        "hashtags": ["Climate", "#CarbonCapture", "CleanTech", "Science", "Energy", "Extra", "DCRBN"],
        "linkedin_angle": "Why decentralized capture matters.",
    }
    caps = g.build_captions(concept, ["tiktok", "youtube", "instagram", "linkedin", "twitter"], brand="DCRBN",
                            website="https://dcrbn.com", credit="labguy", source_url="https://www.tiktok.com/@labguy/video/1")
    for name in ("tiktok", "youtube", "instagram"):
        text = caps[name]
        assert "http" not in text and "dcrbn.com" not in text.lower()
        assert not text.lower().startswith("dcrbn")
        tags = [w for w in text.split() if w.startswith("#")]
        assert 3 <= len(tags) <= 5 and "#DCRBN" not in tags
        assert "@labguy" in text
    assert len(caps["twitter"]) <= 280 and "http" not in caps["twitter"]
    assert len(caps["linkedin"]) > len(caps["tiktok"])
    assert "Why decentralized" in caps["linkedin"]


def test_creator_cooldown_and_repeat_block():
    g.record_post("b1", source_url="https://www.tiktok.com/@wowlab/video/123")
    cands = [
        {"source_url": "https://tiktok.com/@wowlab/video/123"},
        {"source_url": "https://www.tiktok.com/@wowlab/video/999"},
        {"source_url": "https://x.com/other/status/5"},
    ]
    kept, dropped = g.filter_candidates("b1", cands)
    assert [k["source_url"] for k in kept] == ["https://x.com/other/status/5"]
    assert {d["code"] for d in dropped} == {"repeat_source", "creator_cooldown"}
    later = datetime.now(timezone.utc) + timedelta(days=15)
    kept, _ = g.filter_candidates("b1", cands[1:], now=later)
    assert len(kept) == 2  # cooldown over, different URL
    assert "@wowlab" in g.steering_text("b1", "DCRBN")


def test_kill_switch(monkeypatch):
    monkeypatch.setenv("GROWTH_V2", "0")
    cands = [{"source_url": "https://x.com/a/status/1"}]
    assert g.filter_candidates("b1", cands)[0] == cands
    assert g.ensure_publishable("b1", {"raw": "x.mp4", "vertical": "x-9x16.mp4"}, {})[0] is not None


def test_quota_error_skips_platform_and_alerts():
    assert g.note_publish_error("b2", "twitter", "PostProxy 402: wallet balance insufficient, top up")
    assert g.platform_skipped("b2", "twitter")
    assert not g.platform_skipped("b2", "linkedin")
    assert g.recent_alerts("b2")[-1]["kind"] == "x_quota_exhausted"  # wallet error on X = the shared X quota check
    assert g.note_publish_error("b2", "linkedin", "PostProxy 402: wallet balance insufficient, top up")
    assert g.recent_alerts("b2")[-1]["kind"] == "postproxy_quota"
    assert not g.note_publish_error("b2", "facebook", "PostProxy 500: boom")


def test_slot_retry_cap():
    slot = "2026-10-14T08"
    g.note_slot_attempt("b3", slot)
    g.note_slot_attempt("b3", slot, ok=False)
    state = g.load_state("b3")
    state["slots"][slot]["last_at"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    g.save_state("b3", state)
    assert g.slot_retry_due("b3", slot)
    g.note_slot_attempt("b3", slot)
    g.note_slot_attempt("b3", slot, ok=False)
    state = g.load_state("b3")
    state["slots"][slot]["last_at"] = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
    g.save_state("b3", state)
    assert not g.slot_retry_due("b3", slot)  # cap 2


def test_deletion_notes():
    payload = {"platforms": [{"platform": "youtube", "status": "deleted"}, {"platform": "tiktok", "status": "published"}]}
    notes = g.deletion_notes(payload)
    assert len(notes) == 1 and notes[0].startswith("youtube")


def test_refuses_untransformed_without_commentary(tmp_path):
    from app.media import buyer_media_dir

    d = buyer_media_dir("b4")
    (d / "abc123-9x16.mp4").write_bytes(b"x")
    (d / "abc123-16x9.mp4").write_bytes(b"x")
    media = {"source_url": "https://x.com/a/status/1", "raw": "abc123-raw.mp4", "vertical": "abc123-9x16.mp4", "landscape": "abc123-16x9.mp4"}
    assert g.classify_media("b4", media) == "downloaded"
    out, reason = g.ensure_publishable("b4", media, {"hook": "Hi"})
    assert out is None and "Refused" in reason


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg missing")
def test_transform_burns_overlay(tmp_path):
    from app.media import buyer_media_dir

    d = buyer_media_dir("b5")
    for name, size in (("s1-9x16.mp4", "1080x1920"), ("s1-16x9.mp4", "1920x1080")):
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration=8",
                        "-pix_fmt", "yuv420p", str(d / name)], capture_output=True, check=True)
    media = {"source_url": "https://www.tiktok.com/@wowlab/video/1", "raw": "s1-raw.mp4", "vertical": "s1-9x16.mp4", "landscape": "s1-16x9.mp4"}
    copy = {"hook": "This battery charges in five minutes", "commentary": ["It uses a carbon anode.", "No lithium plating at all."], "brand": "DCRBN"}
    out, reason = g.ensure_publishable("b5", media, copy)
    assert reason == "" and out["transformed"] and "-g2-" in out["vertical"]
    assert (d / out["vertical"]).stat().st_size > 1000
    assert "@wowlab" in out["transform"]["credit"]
    assert g.classify_media("b5", out) == "transformed"


def test_brand_clip_is_original():
    assert g.classify_media("b6", {"media_source": "brand_fallback", "vertical": "brand-x-9x16.mp4"}) == "original"


def test_admin_apis_require_secret():
    from app.main import app

    with TestClient(app) as client:
        for path in ("/api/run-guard/summary", "/api/gemini-daily-budget/summary", "/api/growth-v2/status"):
            assert client.get(path).status_code == 401
            assert client.get(path, headers={"X-Admin-Secret": "wrong"}).status_code == 401
            assert client.get(path, headers={"X-Admin-Secret": admin_secret()}).status_code == 200
        assert client.post("/api/run-guard/check", json={}).status_code == 401
        assert client.post("/api/growth-v2/dry-run", json={"buyer_id": "x"}).status_code == 401
        health = client.get("/health").json()
        assert health["growth_v2"]["enabled"] is True


def test_publish_cut_refuses_raw_download(monkeypatch):
    from app import spine
    from app.media import buyer_media_dir

    d = buyer_media_dir("b7")
    (d / "zz-9x16.mp4").write_bytes(b"x")
    calls = []

    async def fake_create(*a, **k):
        calls.append(k)
        return {"id": "p1"}

    monkeypatch.setattr(spine.postproxy, "create_post", fake_create)
    record = {"buyer_id": "b7", "postproxy_api_key": "k", "platforms": ["tiktok"]}
    media = {"source_url": "https://x.com/a/status/1", "raw": "zz-raw.mp4", "vertical": "zz-9x16.mp4", "landscape": "zz-16x9.mp4"}
    res = asyncio.run(spine.publish_cut(record, {"title": "t", "captions": {}}, media))
    assert res["ok"] is False and res.get("refused_untransformed")
    assert calls == []
