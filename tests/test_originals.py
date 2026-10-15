from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from app import growth_v2 as g
from app import originals as o

FFMPEG = shutil.which("ffmpeg")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GROWTH_V2_ORIGINALS", "1")
    monkeypatch.delenv("GROWTH_V2_REPOST_FALLBACK", raising=False)
    monkeypatch.delenv("GROWTH_VIDEO_ENGINES", raising=False)
    monkeypatch.setenv("GROWTH_RETRY_BACKOFF", "0")


RECORD = {"buyer_id": "t1", "brand_name": "DCRBN", "website_url": "https://dcrbn.com", "gemini_api_key": "k"}


def _concept(topic: str, n: int = 0) -> dict:
    return o.sanitize_concept({
        "topic": topic, "topic_key": topic.lower(), "veo_prompt": f"Macro of {topic}, vertical 9:16.",
        "shot_list": [{"t": "0-1s", "shows": "a spark with a crack"}], "on_screen_hook": f"{1000 + n} degrees in 2s",
        "hook": f"Watch {topic} happen in 2 seconds", "lines": ["It is real."], "hashtags": ["#Science"],
        "comment_question": "Would you touch it",
        "title_line": f"The {topic} Showdown",
    })


def test_defaults_repost_off_and_ladder():
    assert o.originals_enabled()
    assert not o.repost_fallback_enabled()
    assert o.engine_ladder() == ["google_veo_lite"] * 3 + ["google_veo_fast"] * 2
    assert o.VEO_MODELS["google_veo_lite"] == "veo-3.1-lite-generate-preview"
    assert o.VEO_MODELS["google_veo_fast"] == "veo-3.1-fast-generate-preview"
    assert o.VISION_MODEL == "gemini-3.6-flash"
    assert o.CLIP_SECONDS == 8


def test_topic_dedupe_loose():
    assert o.topics_similar("Prince Rupert's Drop Shatter", "prince ruperts drop glass")
    assert not o.topics_similar("Ferrofluid spikes", "Thermite rail weld")
    g.record_post("t1", source_url="", topic="Ferrofluid magnet spikes", media_source="ai_generated")
    assert o.is_recent_topic({"topic": "ferrofluid spike crown"}, o.recent_topics("t1"))
    assert o.is_recent_topic({"topic": "Tesla turbine"}, o.recent_topics("t1")) is None


def test_captions_one_unique_title_line_shared_across_platforms():
    c = {**_concept("Thermite"), "series": "Wow in 8 Seconds", "episode": 3}
    caps = g.build_captions(c, ["tiktok", "youtube", "instagram", "facebook", "linkedin", "twitter"], brand="DCRBN")
    for text in caps.values():
        assert "Wow in 8 Seconds" not in text and "#3" not in text
        assert "Would you touch it?" in text
        assert text.startswith("The Thermite Showdown")  # same top line on every platform
    assert caps["tiktok"].splitlines()[1] == c["hook"]
    assert not hasattr(o, "series_name") and not hasattr(o, "DEFAULT_SERIES")
    copy = o.copy_for_original(RECORD, _concept("Thermite"), ["tiktok"])
    assert "series" not in copy and "episode" not in copy and copy["title_line"] == "The Thermite Showdown"


def test_title_cleaning_and_repeat_check():
    assert o.clean_title("Episode 4: Molten Rail Magic #4") == "Molten Rail Magic"
    assert o.title_repeats("Molten rail magic!", ["molten Rail Magic"])
    assert o.title_repeats("The Molten Rail Magic", ["Molten Rail Magic"])
    assert o.title_repeats("", []) == "(empty)"
    assert o.title_repeats("Glass That Fights Back", ["Molten Rail Magic"]) is None
    g.record_post("t1", source_url="", topic="Rail weld", media_source="ai_generated", title_line="Molten Rail Magic")
    assert o.recent_titles("t1") == ["Molten Rail Magic"]


def test_plan_concept_rejects_repeated_title(monkeypatch):
    g.record_post("t1", source_url="", topic="old", media_source="ai_generated", title_line="The Glass Showdown")
    replies = iter([
        {**_concept("Glass"), "title_line": "The Glass Showdown", "veo_prompt": "p", "hook": "Watch glass break in 2 seconds"},
        {**_concept("Ferrofluid"), "veo_prompt": "p", "hook": "Watch ferrofluid spike in 2 seconds"},
    ])
    import json as _json

    async def fake_text(key, prompt, **kw):
        return _json.dumps(next(replies))

    monkeypatch.setattr("app.gemini.generate_text", fake_text)
    c = _run(o.plan_concept(RECORD))
    assert c["title_line"] == "The Ferrofluid Showdown"


def test_x_quota_shared_check():
    msg = "twitter: 24 hour Twitter quota exceeded. Top up your wallet to keep posting or consider using BYOK."
    assert g.is_x_quota_error(msg)
    assert g.is_x_quota_error("24 hour quota exceeded", ["twitter"])
    assert not g.is_x_quota_error("linkedin: 500 boom")
    assert g.note_publish_error("t1", "twitter", msg)
    assert g.recent_alerts("t1")[-1]["kind"] == "x_quota_exhausted"
    assert g.load_state("t1")["x_quota_until"]


def test_origin_tags():
    assert g.origin_of("ai_generated") == "original"
    assert g.origin_of("growth_v2_transformed") == "repost"
    g.record_post("t1", source_url="", topic="x", media_source="ai_generated", post_ids=["p1"])
    assert g.load_state("t1")["posted"][-1]["origin"] == "original"


def test_metrics_normalize():
    assert g.normalize_stats({"likes": 4, "comments": 2, "shares": 1, "impressions": 9}) == {
        "likes": 4, "comments": 2, "shares": 1, "impressions": 9}
    assert g.normalize_stats({"reactions": 3, "reply_count": 1, "retweets": 2})["shares"] == 2


def _run(coro):
    return asyncio.run(coro)


def test_veo_failure_gets_new_topic_and_lite_then_fast(monkeypatch):
    topics = iter(["Topic One", "Topic Two", "Topic Three", "Topic Four", "Topic Five", "Topic Six"])
    seen_tried = []

    async def plan(record, tried=None, niche=""):
        seen_tried.append([c.get("topic") for c in tried or []])
        return _concept(next(topics))

    engines = []

    async def veo(record, prompt, engine, dest):
        engines.append(engine)
        raise o.OriginalFailed(f"{engine}: boom")

    monkeypatch.setattr(o, "plan_concept", plan)
    monkeypatch.setattr(o, "generate_veo_clip", veo)
    with pytest.raises(o.OriginalFailed) as err:
        _run(o.produce_original(RECORD))
    assert engines == ["google_veo_lite"] * 3 + ["google_veo_fast"] * 2
    assert seen_tried[-1] == ["Topic One", "Topic Two", "Topic Three", "Topic Four"]
    assert "All 5" in str(err.value)


def test_mismatch_rewrite_then_regenerate(monkeypatch, tmp_path):
    raw = tmp_path / "r.mp4"
    raw.write_bytes(b"x")
    calls = []

    async def verify(record, path, concept, force_mismatch=False):
        calls.append(concept.get("hook"))
        return {"matches": False, "mismatches": ["no hammer"], "visible_summary": "glass sits still",
                "rewrite": {"on_screen_hook": "Glass at rest", "hook": "This glass just sits there calmly", "lines": ["It stays whole."]}}

    monkeypatch.setattr(o, "verify_clip_matches", verify)
    with pytest.raises(o.OriginalFailed, match="after rewrite"):
        _run(o.ensure_caption_matches(RECORD, raw, _concept("Glass")))
    assert len(calls) == 2


def test_rewrite_keeps_number():
    fixed = o.apply_caption_rewrite(_concept("Glass"), {"on_screen_hook": "Glass at rest", "hook": "This glass just sits there", "lines": ["ok"]})
    assert o.has_number(fixed["on_screen_hook"]) and fixed["caption_rewritten_from_frames"]


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg missing")
def test_full_original_pipeline_forced_failure_and_mismatch(monkeypatch, tmp_path):
    topics = iter(["Topic One", "Topic Two", "Topic Three"])

    async def plan(record, tried=None, niche=""):
        return _concept(next(topics))

    async def veo(record, prompt, engine, dest):
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc=size=720x1280:rate=24:duration=8",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-shortest", "-c:v", "libx264", "-c:a", "aac", str(dest)],
                       capture_output=True, check=True)
        return {"engine": engine, "model": o.VEO_MODELS[engine]}

    async def vision(record, prompt, frames):
        return {"matches": True, "visible_summary": "a test pattern", "mismatches": [], "rewrite": None}

    monkeypatch.setattr(o, "plan_concept", plan)
    monkeypatch.setattr(o, "generate_veo_clip", veo)
    monkeypatch.setattr(o, "vision_json", vision)
    media, concept, info = _run(o.produce_original(RECORD, force_veo_failures=1, force_mismatch=True))
    assert info["attempt"] == 2 and info["engine"] == "google_veo_lite"
    assert info["caption_check"] == "rewritten_from_frames"
    assert info["sfx_added"] is True  # silent opening got the whoosh
    assert info["native_vertical_source"] is True
    assert media["media_source"] == "ai_generated" and g.classify_media("t1", media) == "original"
    assert concept["topic"] == "Topic Two"


def test_run_autopilot_without_repost_never_scans(monkeypatch):
    from app import spine

    async def fail(record, **kw):
        raise o.OriginalFailed("all failed")

    async def no_scan(*a, **k):
        raise AssertionError("repost scan must not run when the repost backup is off")

    monkeypatch.setattr(o, "produce_original", fail)
    monkeypatch.setattr(spine, "scan_trends", no_scan)
    monkeypatch.setattr(spine, "render_brand_clip", lambda bid, rec: {"source_url": "https://dcrbn.com", "media_source": "brand_fallback",
                                                                          "vertical_url": "v", "landscape_url": "l"})

    async def fake_copy(record, scan, mock=False):
        return {"title": "t", "hook": "h", "captions": {}, "topic": "brand"}

    monkeypatch.setattr(spine, "write_copy", fake_copy)
    res = _run(spine.run_autopilot(RECORD, dry_run=True))
    assert res["dry_run"] and res["published"] is False
    assert res["origin"] == "original"
    assert res["skipped_sources"][0]["code"] == "originals_failed"
