"""Regressions from the 2026-10-08 outside-tester QA pass."""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import order_guard, run_guard
from app.hashtags import apply_hashtags, hashtags_for
from app.main import app
from app.onboard import blocked, readiness
from app.store import ensure_buyer, load_buyer, save_buyer


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDER_GUARD_LEDGER", str(tmp_path / "og.json"))
    monkeypatch.setenv("RUN_GUARD_LEDGER", str(tmp_path / "rg.json"))
    order_guard.reset_for_tests()
    yield
    order_guard.reset_for_tests()


def _paid(sid: str, email: str):
    return SimpleNamespace(
        id=sid,
        status="complete",
        payment_status="paid",
        customer_email=email,
        metadata={"email": email, "name": "QA", "source": "agent"},
    )


def _poll(client, sid: str, email: str) -> dict:
    session = _paid(sid, email)
    with patch("app.main.stripe_configured", return_value=True), patch(
        "app.main.retrieve_checkout_session", return_value=session
    ), patch("app.main.session_is_paid", return_value=True), patch(
        "app.main.metadata_from_session", return_value=session.metadata
    ):
        res = client.get("/api/orders/status", params={"session_id": sid})
    assert res.status_code == 200
    return res.json()


def test_second_paid_session_for_same_email_gets_its_own_license(monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    email = f"victim-{uuid.uuid4().hex[:6]}@studio.test"
    with TestClient(app) as client:
        first = _poll(client, "cs_test_first" + uuid.uuid4().hex[:6], email)
        again = _poll(client, "cs_test_other" + uuid.uuid4().hex[:6], email)
    assert first["mcp_url"] != again["mcp_url"]
    assert first["buyer_id"] != again["buyer_id"]
    assert again["buyer_id"].startswith(first["buyer_id"])


def test_same_session_is_still_idempotent_across_status_thanks_and_webhook(monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    email = f"poll-{uuid.uuid4().hex[:6]}@studio.test"
    sid = "cs_test_same" + uuid.uuid4().hex[:6]
    session = _paid(sid, email)
    event = {"id": "evt_" + sid, "type": "checkout.session.completed",
             "data": {"object": {"id": sid, "metadata": session.metadata}}}
    with TestClient(app) as client:
        with patch("app.main.construct_webhook_event", return_value=event), patch(
            "app.main.metadata_from_session", return_value=session.metadata
        ):
            assert client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": "x"}).status_code == 200
        polled = _poll(client, sid, email)
        with patch("app.main.stripe_configured", return_value=True), patch(
            "app.main.retrieve_checkout_session", return_value=session
        ), patch("app.main.session_is_paid", return_value=True), patch(
            "app.main.metadata_from_session", return_value=session.metadata
        ):
            thanks = client.get("/buy/thanks", params={"session_id": sid},
                                headers={"accept": "application/json"}).json()
        polled2 = _poll(client, sid, email)
    assert polled["mcp_url"] == thanks["mcp_url"] == polled2["mcp_url"]
    assert polled["order_id"] == thanks["order_id"] == polled2["order_id"]


def test_fresh_license_cannot_start_keyless_jobs() -> None:
    record = {"buyer_id": "qa-fresh", "brand_name": "", "website_url": "", "brand_voice": ""}
    report = readiness(record)
    assert report["can_scan"] is False and report["can_publish"] is False
    assert blocked(record, need="scan") is not None
    assert blocked(record, need="publish") is not None
    assert report["missing"] == ["website_url"]  # walkthrough still asks for the site first


def test_keys_without_brand_can_scan() -> None:
    record = {"buyer_id": "qa-keys", "gemini_api_key": "g", "postproxy_api_key": "p",
              "postproxy_profile_group_id": "grp"}
    report = readiness(record)
    assert report["can_scan"] and report["can_publish"] and not report["ready"]


def test_buyer_brand_tags_are_not_padded_with_6frame_tags() -> None:
    for platform in ("instagram", "youtube", "linkedin", "facebook", "tiktok"):
        tags = hashtags_for(platform, ["SpecialtyCoffee"], locked=("#BlueBottleCoffee",))
        assert tags == ["#BlueBottleCoffee", "#SpecialtyCoffee"]
    out = apply_hashtags("instagram", "Slow down.", ["Coffee"], locked=())
    assert "#GenerativeFilm" not in out and "#CinematicAI" not in out


def test_partial_publish_is_recorded_and_blocks_a_double_post() -> None:
    import asyncio

    from app import postproxy, spine

    record = {"buyer_id": "qa-partial", "platforms": ["twitter", "facebook"],
              "postproxy_api_key": "k", "postproxy_profile_group_id": "g"}
    copy = {"title": "t", "captions": {"twitter": "same caption here", "facebook": "same caption here"}}
    media = {"vertical_url": "https://h/v.mp4", "landscape_url": "https://h/l.mp4"}

    async def fake_one(rec, cp, name, aspect, url, *, indexed, draft):
        return {"ok": name == "twitter", "platform": name, "aspect": aspect}

    async def fake_profiles(*a, **k):
        return []

    with patch.object(spine, "_publish_one", side_effect=fake_one), patch.object(
        postproxy, "list_profiles", side_effect=fake_profiles
    ):
        first = asyncio.run(spine.publish_cut(record, copy, media, draft=False))
        assert any(p["ok"] for p in first["posts"])
        again = asyncio.run(spine.publish_cut(record, copy, media, draft=False))
    assert again.get("blocked_by_run_guard") is True
