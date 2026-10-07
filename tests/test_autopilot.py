from __future__ import annotations

import os

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-tests")

import pytest

from app.media import MediaError
from app.spine import run_autopilot
from app.store import ensure_buyer, public_config, save_buyer


@pytest.mark.asyncio
async def test_run_autopilot_mock_path() -> None:
    record = ensure_buyer("mock-studio", email="mock@studio.test")
    record["brand_name"] = "North Light"
    record["brand_hashtags"] = ["#NorthLight"]
    record["platforms"] = ["instagram", "youtube", "facebook", "linkedin", "twitter"]
    record = save_buyer(record)
    result = await run_autopilot(record, niche="AI filmmaking", mock=True)
    assert result["ok"] is True
    assert result["mocked"] is True
    assert result["scan"]["source_url"]
    assert result["media"]["vertical"].endswith("9x16.mp4")
    assert result["media"]["landscape"].endswith("16x9.mp4")
    captions = result["copy"]["captions"]
    assert "#NorthLight" in captions["instagram"]
    assert "#6FrameStudio" not in captions["instagram"]
    assert "#Shorts" in result["copy"]["youtube_title"]
    assert "instagram" in result["publish"]["batches"]["vertical_9x16"]
    assert "linkedin" in result["publish"]["batches"]["landscape_16x9"]
    public = public_config(record)
    assert public["gemini_key"] is None
    assert "gemini_api_key" not in public


@pytest.mark.asyncio
async def test_run_autopilot_skips_bot_walled_source(monkeypatch) -> None:
    record = ensure_buyer("kanek-skip", email="kanek@studio.test")
    record["brand_name"] = "KANEK"
    record["platforms"] = ["linkedin", "twitter", "instagram", "youtube"]
    record = save_buyer(record)
    scans = [
        {
            "title": "Short reel",
            "source_url": "https://www.tiktok.com/@x/video/1",
            "platform": "tiktok",
            "why": "short",
            "topic_tags": ["AI"],
            "suggested_start": 0,
            "suggested_duration": 20,
            "notes": "keep the cut",
        },
    ]
    scan_calls = {"n": 0}

    async def fake_scan(record, niche="", mock=False, exclude_urls=None, **kwargs):
        scan_calls["n"] += 1
        return scans.pop(0)

    def fake_download(buyer_id, url, **kwargs):
        if "youtube.com" in url:
            raise MediaError(
                "Could not pull the source clip from youtube.com. "
                "That is the original-video download, not your YouTube channel or PostProxy publish.",
                code="source_bot_check",
            )
        return {
            "source_url": url,
            "mock": False,
            "vertical": "cut-9x16.mp4",
            "landscape": "cut-16x9.mp4",
            "vertical_url": "https://example.test/v",
            "landscape_url": "https://example.test/l",
        }

    async def fake_copy(record, scan, mock=False):
        return {
            "title": scan["title"],
            "youtube_title": f"{scan['title']} #Shorts",
            "topic_tags": ["AI"],
            "captions": {"linkedin": "body", "twitter": "body", "instagram": "body", "youtube": "body"},
        }

    async def fake_publish(record, copy, media, mock=False, draft=False):
        return {"mocked": True, "batches": {}, "posts": []}

    monkeypatch.setattr("app.spine.scan_trends", fake_scan)
    monkeypatch.setattr("app.spine.download_and_cut", fake_download)
    monkeypatch.setattr("app.spine.write_copy", fake_copy)
    monkeypatch.setattr("app.spine.publish_cut", fake_publish)

    result = await run_autopilot(record, source_url="https://www.youtube.com/watch?v=tedxfail")
    assert result["ok"] is True
    assert result["media"]["source_url"] == "https://www.tiktok.com/@x/video/1"
    assert scan_calls["n"] == 1
    assert result["skipped_sources"][0]["code"] == "source_bot_check"
    assert "not your YouTube channel" in result["skipped_sources"][0]["error"]


@pytest.mark.asyncio
async def test_pinned_source_skips_scan(monkeypatch) -> None:
    record = ensure_buyer("pinned-studio", email="pin@studio.test")
    record["platforms"] = ["linkedin"]
    record = save_buyer(record)
    scan_calls = {"n": 0}

    async def fake_scan(*args, **kwargs):
        scan_calls["n"] += 1
        raise AssertionError("scan_trends should not run when source_url is pinned")

    def fake_download(buyer_id, url, **kwargs):
        return {
            "source_url": url,
            "vertical": "cut-9x16.mp4",
            "landscape": "cut-16x9.mp4",
            "vertical_url": "https://example.test/v",
            "landscape_url": "https://example.test/l",
        }

    async def fake_copy(record, scan, mock=False):
        return {
            "title": "pinned",
            "youtube_title": "pinned #Shorts",
            "topic_tags": [],
            "captions": {"linkedin": "body"},
        }

    async def fake_publish(record, copy, media, mock=False, draft=False):
        return {"mocked": True, "batches": {}, "posts": []}

    monkeypatch.setattr("app.spine.scan_trends", fake_scan)
    monkeypatch.setattr("app.spine.download_and_cut", fake_download)
    monkeypatch.setattr("app.spine.write_copy", fake_copy)
    monkeypatch.setattr("app.spine.publish_cut", fake_publish)

    result = await run_autopilot(record, source_url="https://www.tiktok.com/@x/video/9")
    assert result["ok"] is True
    assert scan_calls["n"] == 0
    assert result["scan"]["scanned"] is False
    assert result["media"]["source_url"] == "https://www.tiktok.com/@x/video/9"


@pytest.mark.asyncio
async def test_youtube_wall_skips_later_youtube_and_prefers_x(monkeypatch) -> None:
    record = ensure_buyer("wall-studio", email="wall@studio.test")
    record["platforms"] = ["linkedin"]
    record = save_buyer(record)
    pulled: list[str] = []

    async def fake_scan(*args, **kwargs):
        return {
            "title": "yt first",
            "source_url": "https://www.youtube.com/watch?v=one",
            "candidates": [
                {"title": "yt one", "source_url": "https://www.youtube.com/watch?v=one"},
                {"title": "yt two", "source_url": "https://www.youtube.com/shorts/two"},
                {"title": "reddit", "source_url": "https://www.reddit.com/r/ai/comments/abc/x/"},
                {"title": "x clip", "source_url": "https://x.com/someone/status/123"},
            ],
        }

    def fake_download(buyer_id, url, **kwargs):
        pulled.append(url)
        if "x.com" in url:
            raise MediaError("x down", code="download_failed")
        if "youtube.com" in url:
            raise MediaError("Sign in to confirm you're not a bot", code="source_bot_check")
        if "reddit.com" in url:
            raise MediaError("reddit 403", code="source_bot_check")
        raise AssertionError(url)

    async def fake_copy(record, scan, mock=False):
        return {"title": scan["title"], "youtube_title": "t #Shorts", "topic_tags": [], "captions": {"linkedin": "b"}}

    async def fake_publish(record, copy, media, mock=False, draft=False):
        return {"mocked": True, "batches": {}, "posts": []}

    def fake_brand(buyer_id, record, **kwargs):
        return {
            "source_url": "https://brand.example",
            "vertical": "b-9x16.mp4",
            "landscape": "b-16x9.mp4",
            "vertical_url": "https://example.test/bv",
            "landscape_url": "https://example.test/bl",
            "media_source": "brand_fallback",
        }

    monkeypatch.setattr("app.spine.scan_trends", fake_scan)
    monkeypatch.setattr("app.spine.download_and_cut", fake_download)
    monkeypatch.setattr("app.spine.write_copy", fake_copy)
    monkeypatch.setattr("app.spine.publish_cut", fake_publish)
    monkeypatch.setattr("app.spine.render_brand_clip", fake_brand)
    monkeypatch.delenv("FALLBACK_CLIP_URLS", raising=False)

    result = await run_autopilot(record)
    # Non-YouTube first; only ONE YouTube pull, the second YouTube is skipped without a download.
    assert pulled[0] == "https://x.com/someone/status/123"
    assert pulled[1] == "https://www.reddit.com/r/ai/comments/abc/x/"
    assert sum(1 for url in pulled if "youtube.com" in url) == 1
    codes = [item["code"] for item in result["skipped_sources"]]
    assert "youtube_bot_wall_skip" in codes
    # Nothing pulled -> always-post brand fallback still publishes.
    assert result["ok"] is True
    assert result["media_source"] == "brand_fallback"
    assert result["scan"]["fallback"] is True


def test_order_candidates_puts_youtube_last() -> None:
    from app.spine import order_candidates

    ordered = order_candidates(
        [
            {"source_url": "https://youtu.be/a"},
            {"source_url": "https://www.tiktok.com/@a/video/1"},
            {"source_url": "https://x.com/a/status/1"},
        ]
    )
    assert [c["source_url"] for c in ordered][-1] == "https://youtu.be/a"
    assert ordered[0]["source_url"] == "https://x.com/a/status/1"
    assert all("youtu" not in c["source_url"] for c in order_candidates(ordered, avoid_youtube=True))
