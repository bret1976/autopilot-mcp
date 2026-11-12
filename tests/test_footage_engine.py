"""ORIGINALS_ENGINE=footage: real archival footage, template copy, local voice. No Gemini key, no Gemini call."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from app import footage_mode as fm
from app import footage_topics as ft
from app import growth_v2 as g
from app import originals as o
from app import spine
from app.config import footage_engine, originals_engine, required_api_keys
from app.platforms import split_batches

FFMPEG = shutil.which("ffmpeg")
RECORD = {"buyer_id": "f1", "brand_name": "6Frame Studio", "website_url": "https://6framestudio.com",
          "originals_engine": "footage", "postproxy_api_key": "pp", "postproxy_profile_group_id": "grp"}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROWTH_V2_ORIGINALS", "1")
    monkeypatch.delenv("ORIGINALS_ENGINE", raising=False)
    monkeypatch.delenv("GROWTH_V2_REPOST_FALLBACK", raising=False)


def _no_gemini(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("footage engine must not call Gemini")

    async def aboom(*a, **k):
        boom()

    for mod, names in ((spine, ("generate_json", "generate_json_with_sources")),
                       (o, ("plan_concept", "generate_veo_clip", "ensure_caption_matches", "vision_json"))):
        for n in names:
            monkeypatch.setattr(mod, n, aboom)


def _run(coro):
    return asyncio.run(coro)


def test_engine_is_per_buyer_and_env_default(monkeypatch):
    assert originals_engine({}) == "gemini"
    assert footage_engine({"originals_engine": "footage"})
    monkeypatch.setenv("ORIGINALS_ENGINE", "footage")
    assert footage_engine({}) and footage_engine(None)
    assert originals_engine({"originals_engine": "gemini"}) == "gemini"  # buyer choice beats env default
    assert "gemini_api_key" not in required_api_keys({"originals_engine": "footage"})
    assert "gemini_api_key" in required_api_keys({"originals_engine": "gemini"})


def test_footage_buyer_needs_no_gemini_key():
    from app.onboard import blocked, readiness
    from app.walkthrough import has_apis

    rep = readiness(RECORD)
    assert rep["can_scan"] and "gemini_api_key" not in rep["missing"]
    assert has_apis(RECORD)
    assert blocked(RECORD, need="scan") is None
    gem = {k: v for k, v in RECORD.items() if k != "originals_engine"}
    assert not readiness(gem)["can_scan"] and "gemini_api_key" in readiness(gem)["missing"]


def test_setup_saves_only_valid_engine():
    from app.store import ensure_buyer, update_setup, public_config

    ensure_buyer("f2")
    assert update_setup("f2", {"originals_engine": "FOOTAGE"})["originals_engine"] == "footage"
    assert update_setup("f2", {"originals_engine": "bogus"})["originals_engine"] == "footage"
    assert public_config(update_setup("f2", {"originals_engine": "gemini"}))["originals_engine"] == "gemini"


VIRAL = {"source": "open", "window_hours": 24, "sources": {"youtube_ytdlp": "ok"}, "errors": [],
         "top": [{"title": "Rare D-Day Normandy landing footage restored", "url": "https://www.youtube.com/watch?v=a",
                  "views": 90000, "views_per_hour": 4000.0, "age_hours": 20},
                 {"title": "Cat compilation", "url": "https://www.youtube.com/watch?v=b", "views": 9e6, "views_per_hour": 9e5}]}


def test_scan_trends_uses_open_finder_not_gemini(monkeypatch):
    _no_gemini(monkeypatch)
    monkeypatch.setattr(fm, "todays_viral", lambda queries=None: VIRAL)
    scan = _run(spine.scan_trends(RECORD))
    assert scan["engine"] == "footage" and scan["gemini_calls"] == 0
    assert scan["footage_topic_key"] == "dday-1944" and scan["viral_top"][0]["views_per_hour"] == 4000.0
    assert scan["matched"][0]["url"] == "https://www.youtube.com/watch?v=a"


def test_write_copy_is_template_copy(monkeypatch):
    _no_gemini(monkeypatch)
    copy = _run(spine.write_copy(RECORD, {"title": ft.BY_KEY["titanic-1912"]["topic"]}))
    assert copy["footage_topic_key"] == "titanic-1912" and copy["media_source"] == "archival_footage"
    assert copy["captions"] and copy["gemini_calls"] == 0
    joined = " ".join(copy["captions"].values()).lower()
    assert "ai-generated" not in joined and "made with ai" not in joined
    with pytest.raises(ValueError):
        spine.write_copy_footage(RECORD, {"title": "zzzz qqqq"})


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg missing")
def test_run_autopilot_dry_run_footage_no_gemini(monkeypatch, tmp_path):
    _no_gemini(monkeypatch)
    concept = fm.build_concept(ft.BY_KEY["artemis1-2022"], [{"credit_short": "NASA (public domain)"}])

    def fake_produce(out_dir, avoid_keys=None, recent_topics=None, name=None, **kw):
        v, l = Path(out_dir) / f"{name}.mp4", Path(out_dir) / f"{name}_16x9.mp4"
        for path, size in ((v, "1080x1920"), (l, "1920x1080")):
            subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30",
                            "-t", "2", "-pix_fmt", "yuv420p", str(path)], check=True)
        return str(v), str(l), concept, {"media_source": "archival_footage", "topic_key": "artemis1-2022",
                                         "vertical": {"duration": 8.0}, "landscape": {"duration": 8.0},
                                         "voiceover": {"tts": "kokoro-onnx"}, "footage_sources": []}

    monkeypatch.setattr(fm, "produce_footage", fake_produce)
    res = _run(spine.run_autopilot(RECORD, dry_run=True))
    assert res["dry_run"] and res["published"] is False
    media = res["media"]
    assert media["media_source"] == "archival_footage" and media["engine"] == "footage"
    assert media["cost_usd"] == 0.0 and media["gemini_calls"] == 0
    assert ".mp4?" in media["vertical_url"] and "_16x9.mp4?" in media["landscape_url"]
    assert res["origin"] == "original" and res["media_kind"] == "original"
    assert not any(k for k in media if "ai_generated" in k or k in {"is_ai_generated", "made_with_ai", "ai_disclosure"})


def test_landscape_goes_to_linkedin_and_x():
    b = split_batches(["tiktok", "instagram", "youtube", "linkedin", "twitter"])
    assert set(b["landscape_16x9"]) == {"linkedin", "twitter"}
    assert "linkedin" not in b["vertical_9x16"]


def test_origin_of_archival_is_original():
    assert g.origin_of("archival_footage") == "original"
    assert g.classify_media("f1", {"media_source": "archival_footage"}) == "original"


def test_health_reports_footage(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setenv("ORIGINALS_ENGINE", "footage")
    body = TestClient(app).get("/health").json()
    assert body["footage_mode"]["enabled"] is True and body["footage_mode"]["default_originals_engine"] == "footage"


def test_admin_footage_check_requires_secret_and_saves_nothing(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.store import ensure_buyer, load_buyer, update_setup

    _no_gemini(monkeypatch)
    monkeypatch.setattr(fm, "todays_viral", lambda queries=None: VIRAL)
    ensure_buyer("f3")
    update_setup("f3", {"brand_name": "X"})
    c = TestClient(app)
    assert c.post("/api/footage/check", json={"buyer_id": "f3"}).status_code == 401
    r = c.post("/api/footage/check", json={"buyer_id": "f3"}, headers={"x-admin-secret": "test-admin"}).json()
    assert r["ok"] and r["scan"]["engine"] == "footage" and r["copy"]["gemini_calls"] == 0
    assert "originals_engine" not in load_buyer("f3")
