from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import order_guard
from app.main import app
from app.store import list_leads


@pytest.fixture(autouse=True)
def _fresh_guard(tmp_path, monkeypatch):
    monkeypatch.setenv("ORDER_GUARD_LEDGER", str(tmp_path / "og.json"))
    order_guard.reset_for_tests()
    yield
    order_guard.reset_for_tests()


def _paid_session(sid: str, email: str):
    return SimpleNamespace(
        id=sid,
        status="complete",
        payment_status="paid",
        customer_email=email,
        metadata={"email": email, "name": "Poll Buyer", "source": "agent"},
    )


def _count(email: str) -> int:
    return sum(1 for row in list_leads(limit=5000) if row.get("email") == email)


def test_health_lists_pack() -> None:
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["packs"]["order_guard"] == "order-guard-v1"
    assert body["packs"]["run_guard"] == "run-guard-v1"
    assert body["order_guard"]["enabled"] is True


def test_status_polls_write_one_order(monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    email = "poller@studio.test"
    session = _paid_session("cs_test_poll1", email)
    with patch("app.main.stripe_configured", return_value=True), patch(
        "app.main.retrieve_checkout_session", return_value=session
    ), patch("app.main.session_is_paid", return_value=True), patch(
        "app.main.metadata_from_session", return_value=session.metadata
    ):
        before = _count(email)
        with TestClient(app) as client:
            urls = set()
            ids = set()
            for _ in range(5):
                res = client.get("/api/orders/status", params={"session_id": "cs_test_poll1"})
                assert res.status_code == 200
                body = res.json()
                urls.add(body["mcp_url"])
                ids.add(body["order_id"])
    assert len(urls) == 1 and len(ids) == 1
    assert _count(email) - before == 1
    assert order_guard.summary()["duplicate_fulfillments_suppressed"] == 4


def test_webhook_retry_is_deduped(monkeypatch) -> None:
    email = "hook@studio.test"
    event = {
        "id": "evt_test_1",
        "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_test_hook1", "metadata": {"email": email, "name": "Hook"}}},
    }
    with patch("app.main.construct_webhook_event", return_value=event), patch(
        "app.main.metadata_from_session", return_value={"email": email, "name": "Hook"}
    ):
        before = _count(email)
        with TestClient(app) as client:
            first = client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": "x"})
            second = client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": "x"})
    assert first.status_code == 200 and "duplicate" not in first.json()
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert _count(email) - before == 1


def test_webhook_then_poll_same_session_one_order(monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    email = "both@studio.test"
    event = {
        "id": "evt_test_2",
        "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_test_both", "metadata": {"email": email}}},
    }
    session = _paid_session("cs_test_both", email)
    before = _count(email)
    with patch("app.main.construct_webhook_event", return_value=event), patch(
        "app.main.metadata_from_session", return_value={"email": email, "name": "Both"}
    ), patch("app.main.stripe_configured", return_value=True), patch(
        "app.main.retrieve_checkout_session", return_value=session
    ), patch("app.main.session_is_paid", return_value=True):
        with TestClient(app) as client:
            client.post("/api/stripe/webhook", content=b"{}", headers={"stripe-signature": "x"})
            res = client.get("/api/orders/status", params={"session_id": "cs_test_both"})
    assert res.status_code == 200 and res.json()["mcp_url"]
    assert _count(email) - before == 1


def test_admin_login_lockout(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_LOCK_MAX", "3")
    with TestClient(app) as client:
        for _ in range(3):
            assert client.post("/admin/login", data={"password": "nope"}).status_code == 401
        locked = client.post("/admin/login", data={"password": "test-admin"}, follow_redirects=False)
        assert locked.status_code == 429
        assert int(locked.headers["retry-after"]) > 0
        assert client.get("/admin", params={"secret": "test-admin"}).status_code == 429
        res = client.post(
            "/api/orders",
            json={"name": "N", "email": "lock@studio.test"},
            headers={"X-Admin-Secret": "test-admin"},
        )
        assert res.status_code == 429
    assert order_guard.summary()["admin_lockouts"] == 1


def test_admin_login_success_still_works() -> None:
    with TestClient(app) as client:
        assert client.post("/admin/login", data={"password": "wrong"}).status_code == 401
        ok = client.post("/admin/login", data={"password": "test-admin"}, follow_redirects=False)
        assert ok.status_code == 303
        assert client.get("/admin").status_code == 200


def test_admin_bypass_wrong_header_counts_but_no_secret_is_free() -> None:
    with TestClient(app) as client:
        res = client.post("/api/orders", json={"name": "N", "email": "free@studio.test"})
        assert res.status_code in (402, 503)
    assert order_guard.summary()["admin_failures"] == 0
    with TestClient(app) as client:
        client.post(
            "/api/orders",
            json={"name": "N", "email": "bad@studio.test"},
            headers={"X-Admin-Secret": "guess"},
        )
    assert order_guard.summary()["admin_failures"] == 1


def test_checkout_creation_throttled(monkeypatch) -> None:
    monkeypatch.setenv("ORDER_CHECKOUT_PER_HOUR", "2")
    fake = {
        "session_id": "cs_test_t",
        "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_t",
        "buyer_id": "t",
    }
    with patch("app.main.create_checkout_session", return_value=fake) as created, patch(
        "app.main.stripe_configured", return_value=True
    ):
        with TestClient(app) as client:
            codes = [
                client.post("/api/orders", json={"name": "T", "email": f"t{i}@studio.test"}).status_code
                for i in range(3)
            ]
            html = client.post(
                "/api/orders",
                data={"name": "T", "email": "th@studio.test"},
                headers={"Accept": "text/html"},
                follow_redirects=False,
            )
    assert codes == [402, 402, 429]
    assert html.status_code == 429
    assert created.call_count == 2


def test_status_poll_throttled(monkeypatch) -> None:
    monkeypatch.setenv("ORDER_STATUS_PER_MIN", "2")
    with TestClient(app) as client:
        codes = [
            client.get("/api/orders/status", params={"session_id": "cs_test_x"}).status_code
            for _ in range(3)
        ]
    assert codes[2] == 429


def test_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("ORDER_GUARD", "0")
    monkeypatch.setenv("ADMIN_LOCK_MAX", "1")
    with TestClient(app) as client:
        for _ in range(3):
            assert client.post("/admin/login", data={"password": "nope"}).status_code == 401
        ok = client.post("/admin/login", data={"password": "test-admin"}, follow_redirects=False)
        assert ok.status_code == 303


def test_ledger_caps(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(order_guard, "MAX_EVENTS", 5)
    for i in range(9):
        order_guard.mark_event(f"evt_{i}")
    data = json.loads((tmp_path / "og.json").read_text())
    assert len(data["events"]) == 5
    assert "evt_8" in data["events"] and "evt_0" not in data["events"]
