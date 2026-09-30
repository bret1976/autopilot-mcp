from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.main import app
from app.tokens import verify_token


def test_orders_stripe_checkout_required(monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    fake = {
        "session_id": "cs_test_123",
        "checkout_url": "https://checkout.stripe.com/c/pay/cs_test_123",
        "buyer_id": "nia-at-studio-test",
        "price_usd": 997,
        "sku": "autopilot-mcp-license",
    }
    with patch("app.main.create_checkout_session", return_value=fake):
        with patch("app.main.stripe_configured", return_value=True):
            with patch("app.agent_discovery.stripe_configured", return_value=True):
                with patch("app.agent_discovery.payment_mode", return_value="stripe_checkout_then_mint"):
                    with TestClient(app) as client:
                        res = client.post(
                            "/api/orders",
                            json={"name": "Nia", "email": "nia@studio.test", "source": "agent"},
                        )
                        assert res.status_code == 402
                        body = res.json()
                        assert body["ok"] is False
                        assert body["payment_required"] is True
                        assert body["checkout_url"] == fake["checkout_url"]
                        assert body["session_id"] == fake["session_id"]
                        assert body["status_url"]
                        assert "cs_test_123" in body["status_url"]
                        assert body["price_usd"] == 997
                        assert "mcp_url" not in body or not body.get("mcp_url")

                        html = client.post(
                            "/api/orders",
                            data={"name": "Nia", "email": "nia2@studio.test", "client": "Grok"},
                            headers={"Accept": "text/html"},
                            follow_redirects=False,
                        )
                        assert html.status_code == 303
                        assert html.headers["location"] == fake["checkout_url"]


def test_thanks_fulfills_when_paid(monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    session = SimpleNamespace(
        id="cs_test_paid",
        payment_status="paid",
        customer_email="paid@studio.test",
        metadata={
            "name": "Paid Buyer",
            "email": "paid@studio.test",
            "source": "stripe",
            "client": "Claude.ai",
            "studio": "",
        },
    )
    with patch("app.main.stripe_configured", return_value=True):
        with patch("app.main.retrieve_checkout_session", return_value=session):
            with TestClient(app) as client:
                res = client.get("/buy/thanks?session_id=cs_test_paid")
                assert res.status_code == 200
                assert "/mcp/t/" in res.text
                assert "Copy the URL" in res.text


def test_webhook_fulfills_on_completed(monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_wh",
                "payment_status": "paid",
                "customer_email": "hook@studio.test",
                "metadata": {
                    "name": "Hook Buyer",
                    "email": "hook@studio.test",
                    "source": "stripe_webhook",
                    "client": "",
                    "studio": "",
                },
            }
        },
    }
    with patch("app.main.construct_webhook_event", return_value=event):
        with TestClient(app) as client:
            res = client.post(
                "/api/stripe/webhook",
                content=b"{}",
                headers={"stripe-signature": "t=1,v1=fake"},
            )
            assert res.status_code == 200
            assert res.json()["ok"] is True
            # Admin path still works; minted buyer should exist via fulfill
            minted = client.post(
                "/api/orders",
                json={"name": "Hook Buyer", "email": "hook@studio.test"},
                headers={"X-Admin-Secret": "test-admin"},
            )
            assert minted.status_code == 200
            assert "/mcp/t/" in minted.json()["mcp_url"]
            token = minted.json()["mcp_url"].rstrip("/").rsplit("/", 1)[1]
            assert verify_token(token) is not None


def test_orders_status_unpaid(monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    session = SimpleNamespace(
        id="cs_test_open",
        payment_status="unpaid",
        status="open",
        customer_email="wait@studio.test",
        metadata={"name": "Wait", "email": "wait@studio.test", "buyer_id": "wait-at-studio-test"},
        get=lambda k, default=None: getattr(session, k, default) if False else None,
    )
    # session.get used by metadata/session_is_paid via getattr primarily
    with patch("app.main.stripe_configured", return_value=True):
        with patch("app.main.retrieve_checkout_session", return_value=session):
            with TestClient(app) as client:
                res = client.get("/api/orders/status", params={"session_id": "cs_test_open"})
                assert res.status_code == 200
                body = res.json()
                assert body["payment_required"] is True
                assert body["mcp_url"] is None
                assert body["session_id"] == "cs_test_open"


def test_orders_status_paid_mints(monkeypatch) -> None:
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    session = SimpleNamespace(
        id="cs_test_paid_status",
        payment_status="paid",
        status="complete",
        customer_email="statuspaid@studio.test",
        metadata={
            "name": "Status Paid",
            "email": "statuspaid@studio.test",
            "source": "agent",
            "client": "",
            "studio": "",
        },
    )
    with patch("app.main.stripe_configured", return_value=True):
        with patch("app.main.retrieve_checkout_session", return_value=session):
            with TestClient(app) as client:
                res = client.get(
                    "/api/orders/status",
                    params={"session_id": "cs_test_paid_status"},
                    headers={"Accept": "application/json"},
                )
                assert res.status_code == 200
                body = res.json()
                assert body["ok"] is True
                assert "/mcp/t/" in body["mcp_url"]
                assert body["session_id"] == "cs_test_paid_status"
