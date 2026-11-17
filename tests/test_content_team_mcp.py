from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.mcp_server import mcp
from app.tokens import mint_token

BEFORE = Path("/workspace/ma-content-team/prod-evidence/autopilot_mcp_tools_before.json")
ORIGINAL_TOOLS = [
    "onboard", "start", "setup", "configure", "set_brand_from_website",
    "postproxy_status", "postproxy_connect", "pick_posting_spot", "scan_trends",
    "download_original", "write_copy", "publish", "run_autopilot", "dry_run",
    "choose_channels", "set_automation", "confirm_schedule", "choose_start",
    "approve_and_publish", "status", "proof_link", "last_run",
]
NEW_TOOLS = [
    "content_team_health", "content_team_run", "content_team_drafts",
    "content_team_review", "content_team_ingest", "content_team_report",
]


def _tool_names(tools) -> list[str]:
    return [t.name for t in tools]


def test_existing_tools_were_not_removed_or_renamed() -> None:
    import asyncio
    names = asyncio.run(_list())
    for name in ORIGINAL_TOOLS:
        assert name in names, f"existing tool missing or renamed: {name}"
    # original 22 stay in the same relative order
    original_positions = [names.index(n) for n in ORIGINAL_TOOLS]
    assert original_positions == sorted(original_positions)
    for name in NEW_TOOLS:
        assert name in names, f"new Content Team tool missing: {name}"
    if BEFORE.exists():
        before = json.loads(BEFORE.read_text())["names"]
        assert before == ORIGINAL_TOOLS
        assert names[: len(before)] == before


async def _list() -> list[str]:
    tools = await mcp.list_tools()
    return [t.name for t in tools]


def test_content_team_run_and_review_are_draft_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONTENT_TEAM_LLM", "stub")
    monkeypatch.setenv("DISABLE_BACKGROUND_SCHEDULER", "true")
    token = mint_token("ct-studio")
    with TestClient(app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        body = health.json()
        assert body["ok"] is True
        assert body["packs"]["content_team"]
        assert body["content_team"]["publish_blocked"] is True
        # unauthenticated HTTP is rejected
        denied = client.get("/api/content-team/health")
        assert denied.status_code == 401
        headers = {"Authorization": f"Bearer {token}"}
        ct = client.get("/api/content-team/health", headers=headers)
        assert ct.status_code == 200
        assert ct.json()["publish_blocked"] is True
        run = client.post(
            "/api/content-team/run/writer",
            headers=headers,
            json={"brief": "TEST_DEPLOY_VERIFY: ship in a bottle lighting test."},
        )
        assert run.status_code == 200
        report = run.json()["report"]
        assert run.json()["published"] is False
        assert run.json()["sent"] is False
        assert report["llm_status"] in {"stub", "real", "fallback"}
        draft_id = report["draft_id"]
        assert "TEST_DEPLOY_VERIFY" in (report.get("payload") or {}).get("brief", "") or "TEST_DEPLOY_VERIFY" in (report.get("body") or "") or draft_id
        reviewed = client.post(
            f"/api/content-team/drafts/{draft_id}/review",
            headers=headers,
            json={"status": "REJECTED"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["published"] is False
        assert reviewed.json()["sent"] is False
        assert reviewed.json()["draft"]["status"] == "REJECTED"
        ingested = client.post(
            "/api/content-team/ingest",
            headers=headers,
            json={"kind": "trending", "items": [{"text": "TEST_DEPLOY_VERIFY ingest item", "url": "https://example.com"}]},
        )
        assert ingested.status_code == 200
        assert ingested.json()["added"] == 1
        assert ingested.json()["published"] is False
        assert ingested.json()["sent"] is False


def test_content_team_token_ingests_into_owner_buyer_not_test_licenses(tmp_path, monkeypatch) -> None:
    from app.config import OWNER_BUYER_ID

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CONTENT_TEAM_TOKEN", "routine-token-for-tests")
    monkeypatch.setenv("DISABLE_BACKGROUND_SCHEDULER", "true")
    headers = {"X-Content-Team-Token": "routine-token-for-tests"}
    item = {
        "id": "x-trend-1",
        "url": "https://x.com/studio/status/1",
        "author": "studio",
        "author_name": "Studio",
        "text": "A last-24h AI filmmaking trend.",
        "created_at": "2026-11-17T16:00:00Z",
        "source": "grok_routine",
    }
    with TestClient(app) as client:
        denied = client.post("/api/content-team/ingest", json={"kind": "trending", "items": [item]})
        assert denied.status_code == 401
        posted = client.post(
            "/api/content-team/ingest",
            headers=headers,
            json={"kind": "trending", "source": "grok_routine", "items": [item]},
        )
        assert posted.status_code == 200
        body = posted.json()
        assert body["ok"] is True
        assert body["added"] == 1
        assert body["published"] is False
        assert body["sent"] is False
        listed = client.get("/api/content-team/ingest/trending", headers=headers)
        assert listed.status_code == 200
        payload = listed.json()
        assert payload["ok"] is True
        assert payload["buyer_id"] == OWNER_BUYER_ID
        assert payload["total"] >= 1
        assert payload["published"] is False
        assert payload["sent"] is False
        assert any(it.get("id") == "x-trend-1" for it in payload["items"])
    stored = tmp_path / "buyers" / OWNER_BUYER_ID / "content_team" / "ingest" / "trending.json"
    assert stored.is_file()
    # A test license must not receive the routine ingest.
    test_dir = tmp_path / "buyers" / "test-deploy-verify-20261117" / "content_team" / "ingest" / "trending.json"
    assert not test_dir.exists()
