from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("MCP_ISSUER_SECRET", "test-issuer-secret")
os.environ.setdefault("DATA_DIR", "/tmp/autopilot-mcp-proof-tests")
os.environ.setdefault("PUBLIC_BASE_URL", "http://testserver")

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.proof import all_live_confirmed, build_proof_dashboard, render_proof_html
from app.postproxy import platform_outcomes, result_post_id
from app.spine import publish_cut
from app.store import ensure_buyer, save_buyer
from app import postproxy


def test_result_and_platform_helpers() -> None:
    assert result_post_id({"id": "post_1"}) == "post_1"
    rows = platform_outcomes(
        {
            "id": "post_1",
            "platforms": [
                {
                    "platform": "linkedin",
                    "status": "published",
                    "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:1",
                }
            ],
        }
    )
    assert rows[0]["permalink"].startswith("https://www.linkedin.com")


def test_all_live_confirmed() -> None:
    posts = [{"ok": True}, {"ok": True}]
    assert all_live_confirmed(posts, draft=False) is True
    assert all_live_confirmed(posts, draft=True) is False
    assert all_live_confirmed([{"ok": True}, {"ok": False}], draft=False) is False
    assert all_live_confirmed([], draft=False) is False


@pytest.mark.asyncio
async def test_dashboard_only_after_every_social_confirms(monkeypatch) -> None:
    record = ensure_buyer("proof-studio")
    record["brand_name"] = "Cory Connects"
    record["website_url"] = "https://coryconnects.tech/"
    record["postproxy_api_key"] = "pp-test"
    record = save_buyer(record)
    published = {
        "posts": [
            {
                "ok": True,
                "platform": "linkedin",
                "account": "Cory Connects",
                "result": {
                    "id": "post_li",
                    "platforms": [
                        {
                            "platform": "linkedin",
                            "status": "published",
                            "permalink": "https://www.linkedin.com/feed/update/urn:li:activity:1",
                        }
                    ],
                },
            },
            {
                "ok": True,
                "platform": "twitter",
                "account": "Cory Connects",
                "result": {
                    "id": "post_x",
                    "platforms": [
                        {
                            "platform": "twitter",
                            "status": "published",
                            "permalink": "https://x.com/cory/status/1",
                        }
                    ],
                },
            },
        ]
    }

    async def fake_get(api_key, post_id):
        return published["posts"][0]["result"] if post_id == "post_li" else published["posts"][1]["result"]

    async def fake_capture(*, platform, permalink, dest, poster=None):
        Path(dest).write_bytes(b"\xff\xd8\xff" + b"shot" * 2000)
        return {"ok": True, "path": Path(dest).name, "source": "live_screenshot", "permalink": permalink}

    monkeypatch.setattr(postproxy, "get_post", fake_get)
    built = await build_proof_dashboard(
        record,
        published,
        copy={"title": "Signal cut"},
        media={},
        draft=False,
        capture=fake_capture,
    )
    assert built and built["ok"] is True
    assert built["all_confirmed"] is True
    assert built["url"].startswith("http://testserver/proof/")
    assert built["url"] in built["say_to_user"]
    html = render_proof_html(
        {
            "brand_name": "Cory Connects",
            "title": "Signal cut",
            "posted_at": "now",
            "platforms": built["platforms"],
        }
    )
    assert "Posted on 2 socials" in html
    assert "linkedin.jpg" in html

    client = TestClient(app)
    proof_id = built["id"]
    page = client.get(f"/proof/{proof_id}")
    assert page.status_code == 200
    assert "Cory Connects" in page.text
    assert "Open live" in page.text
    shot = client.get(f"/proof/{proof_id}/linkedin.jpg")
    assert shot.status_code == 200


@pytest.mark.asyncio
async def test_processing_permalink_still_gets_a_proof_link(monkeypatch) -> None:
    record = ensure_buyer("proof-processing")
    record["postproxy_api_key"] = "pp-test"
    record = save_buyer(record)
    published = {
        "posts": [
            {
                "ok": True,
                "platform": "instagram",
                "account": "Cory Connects",
                "result": {
                    "id": "post_ig",
                    "platforms": [{"platform": "instagram", "status": "processing", "permalink": None}],
                },
            }
        ]
    }

    async def fake_get(api_key, post_id):
        return published["posts"][0]["result"]

    async def fake_capture(*, platform, permalink, dest, poster=None):
        Path(dest).write_bytes(b"\xff\xd8\xff" + b"shot" * 2000)
        return {"ok": True, "path": Path(dest).name, "source": "receipt", "permalink": permalink}

    monkeypatch.setattr(postproxy, "get_post", fake_get)
    built = await build_proof_dashboard(
        record, published, copy={"title": "Cut"}, draft=False, capture=fake_capture
    )
    assert built and built["ok"] is True
    assert built["url"]


@pytest.mark.asyncio
async def test_no_dashboard_when_one_social_fails() -> None:
    record = ensure_buyer("proof-fail")
    published = {"posts": [{"ok": True, "platform": "linkedin"}, {"ok": False, "platform": "twitter"}]}
    built = await build_proof_dashboard(record, published, draft=False)
    assert built is None


@pytest.mark.asyncio
async def test_publish_cut_attaches_proof_when_live(monkeypatch) -> None:
    record = ensure_buyer("proof-publish")
    record["postproxy_api_key"] = "pp-test"
    record["postproxy_profile_group_id"] = "grp"
    record["brand_name"] = "Cory Connects"
    record["platforms"] = ["twitter"]
    record = save_buyer(record)
    copy = {"title": "Cut", "youtube_title": "Cut #Shorts", "captions": {"twitter": "body"}}
    media = {"landscape_url": "https://example.test/l.mp4", "vertical_url": "https://example.test/v.mp4"}

    async def fake_profiles(api_key, group=""):
        return {"data": [{"id": "prof_x", "name": "Cory Connects", "platform": "twitter"}]}

    async def fake_create(api_key, **kwargs):
        return {
            "id": "post_live",
            "platforms": [
                {
                    "platform": "twitter",
                    "status": "published",
                    "permalink": "https://x.com/cory/status/9",
                }
            ],
        }

    async def fake_get(api_key, post_id):
        return await fake_create(api_key)

    async def fake_capture(*, platform, permalink, dest, poster=None):
        Path(dest).write_bytes(b"\xff\xd8\xff" + b"x" * 2000)
        return {"ok": True, "path": Path(dest).name, "source": "live_screenshot", "permalink": permalink}

    monkeypatch.setattr(postproxy, "list_profiles", fake_profiles)
    monkeypatch.setattr(postproxy, "create_post", fake_create)
    monkeypatch.setattr(postproxy, "get_post", fake_get)
    monkeypatch.setattr("app.spine.build_proof_dashboard", build_proof_dashboard)
    monkeypatch.setattr("app.proof.capture_platform_visual", fake_capture)

    result = await publish_cut(record, copy, media, mock=False, draft=False)
    assert result["posts"][0]["ok"] is True
    assert result["proof"]["ok"] is True
    assert result["proof"]["url"]
    assert result["proof"]["url"] in result["proof"]["say_to_user"]
