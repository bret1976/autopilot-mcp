from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app import last30days_scan as ls
from app import spine
from app.mcp_server import mcp


NOW = datetime(2026, 11, 17, 16, 0, tzinfo=timezone.utc)
ORIGINAL_28 = [
    "onboard", "start", "setup", "configure", "set_brand_from_website",
    "postproxy_status", "postproxy_connect", "pick_posting_spot", "scan_trends",
    "download_original", "write_copy", "publish", "run_autopilot", "dry_run",
    "choose_channels", "set_automation", "confirm_schedule", "choose_start",
    "approve_and_publish", "status", "proof_link", "last_run",
    "content_team_health", "content_team_run", "content_team_drafts",
    "content_team_review", "content_team_ingest", "content_team_report",
]


def _fresh(hours, title="Fresh HN story", url="https://news.ycombinator.com/item?id=99"):
    published = NOW - timedelta(hours=hours)
    return {
        "objectID": "99",
        "title": title,
        "url": "https://example.com/post",
        "author": "pg",
        "points": 1200,
        "num_comments": 200,
        "created_at_i": int(published.timestamp()),
    }


def test_age_filter_drops_items_older_than_24h(monkeypatch):
    stale = _fresh(30, title="Old", url="https://news.ycombinator.com/item?id=1")
    stale["objectID"] = "1"
    fresh = _fresh(5)

    def fake_algolia(url):
        return {"hits": [stale, fresh]}

    monkeypatch.setattr(ls, "_algolia_json", fake_algolia)
    monkeypatch.setattr(ls, "_fetch_google_trends", lambda now, window_hours=24: ([], "ok"))
    monkeypatch.setattr(ls, "_fetch_reddit_last30days", lambda now, window_hours=24: ([], "schema-drift"))
    found = ls.scan(now=NOW)
    assert found["gemini_calls"] == 0
    urls = [it["url"] for it in found["items"]]
    assert urls == ["https://news.ycombinator.com/item?id=99"]
    assert found["items"][0]["age_hours"] == 5.0
    assert found["items"][0]["platform"] == "hackernews"


def test_scan_result_keeps_scan_trends_shape(monkeypatch):
    monkeypatch.setattr(
        ls, "scan",
        lambda niche="", **k: {
            "engine": "last30days", "window_hours": 24, "sources": {"hackernews": "ok"}, "errors": [],
            "items": [{
                "title": "Qwen4-Max", "url": "https://news.ycombinator.com/item?id=5",
                "source_url": "https://news.ycombinator.com/item?id=5",
                "platform": "hackernews", "author": "x", "published_at": "2026-11-17T10:00:00Z",
                "age_hours": 6, "score": 1400, "comments": 90, "pace": 233,
                "why": "1,400 points in 6 hours on Hacker News.",
            }],
        },
    )
    found = ls.scan()
    result = ls.to_scan_result(found)
    for key in spine.SCAN_FIELDS:
        assert key in result
    assert result["source_url"].startswith("https://")
    assert result["engine"] == "last30days"
    assert result["gemini_calls"] == 0
    assert result["candidates"][0]["title"] == "Qwen4-Max"


def test_scan_trends_never_calls_gemini(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("scan_trends must not call Gemini")

    monkeypatch.setattr(spine, "generate_json", boom)
    monkeypatch.setattr(spine, "generate_json_with_sources", boom)
    monkeypatch.setattr(
        "app.last30days_scan.scan",
        lambda niche="": {
            "engine": "last30days", "window_hours": 24, "sources": {"hackernews": "ok"}, "errors": [],
            "items": [{
                "title": "Measles outbreak", "url": "https://www.nbcnews.com/health/a",
                "source_url": "https://www.nbcnews.com/health/a",
                "platform": "google_trends", "author": "NBC News",
                "published_at": "2026-11-17T16:20:00Z", "age_hours": 1.5,
                "score": 200, "comments": 0, "pace": 133,
                "why": "Google Trends daily search (200+) in the last 1.5 hours.",
            }],
        },
    )
    record = {"buyer_id": "scan-studio", "brand_name": "6Frame", "originals_engine": "gemini"}
    result = asyncio.run(spine.scan_trends(record, niche=""))
    assert result["engine"] == "last30days" and result["gemini_calls"] == 0
    assert result["title"] == "Measles outbreak"
    assert result["source_url"] == "https://www.nbcnews.com/health/a"
    for key in spine.SCAN_FIELDS:
        assert key in result


def test_tool_list_is_still_the_same_28_names():
    names = asyncio.run(_names())
    assert names == ORIGINAL_28


async def _names():
    tools = await mcp.list_tools()
    return [t.name for t in tools]
