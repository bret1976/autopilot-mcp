"""Stripe Checkout Session helpers — mint only after paid (or admin bypass)."""
from __future__ import annotations

from typing import Any

from fastapi import Request

from app.config import (
    PRICE_USD,
    PRODUCT_NAME,
    public_base_url,
    stripe_price_id,
    stripe_secret_key,
    stripe_webhook_secret,
)
from app.orders import buyer_id_from_email


AMOUNT_CENTS = PRICE_USD * 100
SKU_ID = "autopilot-mcp-license"


def _public_base(request: Request | None = None) -> str:
    base = public_base_url().rstrip("/")
    if request is not None:
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https")
        proto = proto.split(",")[0].strip()
        if proto == "https" and base.startswith("http://"):
            base = "https://" + base[len("http://") :]
    return base


def _stripe():
    import stripe

    key = stripe_secret_key()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    stripe.api_key = key
    return stripe


def create_checkout_session(
    *,
    name: str,
    email: str,
    client: str = "",
    studio: str = "",
    source: str = "buy",
    request: Request | None = None,
) -> dict[str, Any]:
    stripe = _stripe()
    buyer_id = buyer_id_from_email(email)
    base = _public_base(request)
    price_id = stripe_price_id()
    if price_id:
        line_items: list[dict[str, Any]] = [{"price": price_id, "quantity": 1}]
    else:
        line_items = [
            {
                "quantity": 1,
                "price_data": {
                    "currency": "usd",
                    "unit_amount": AMOUNT_CENTS,
                    "product_data": {
                        "name": f"{PRODUCT_NAME} License",
                        "description": f"One-time {PRICE_USD} USD TrendPilot license (SKU {SKU_ID})",
                    },
                },
            }
        ]
    session = stripe.checkout.Session.create(
        mode="payment",
        customer_email=email,
        line_items=line_items,
        success_url=f"{base}/buy/thanks?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base}/buy",
        metadata={
            "name": name[:200],
            "email": email[:200],
            "buyer_id": buyer_id,
            "source": (source or "buy")[:80],
            "client": (client or "")[:120],
            "studio": (studio or "")[:120],
            "sku": SKU_ID,
            "price_usd": str(PRICE_USD),
        },
        payment_intent_data={
            "metadata": {
                "buyer_id": buyer_id,
                "sku": SKU_ID,
            }
        },
    )
    return {
        "session_id": session.id,
        "checkout_url": session.url,
        "buyer_id": buyer_id,
        "price_usd": PRICE_USD,
        "sku": SKU_ID,
    }


def construct_webhook_event(payload: bytes, signature: str | None):
    stripe = _stripe()
    secret = stripe_webhook_secret()
    if not secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET is not configured")
    if not signature:
        raise ValueError("missing Stripe-Signature header")
    return stripe.Webhook.construct_event(payload, signature, secret)


def retrieve_checkout_session(session_id: str) -> Any:
    stripe = _stripe()
    return stripe.checkout.Session.retrieve(session_id)


def _stripe_get(obj: Any, key: str, default: Any = None) -> Any:
    """Read a field from a StripeObject or plain dict without dict(StripeObject)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        val = getattr(obj, key, default)
    except Exception:  # noqa: BLE001
        return default
    return default if val is None else val


def session_is_paid(session: Any) -> bool:
    status = str(_stripe_get(session, "payment_status") or "")
    return status == "paid"


def metadata_from_session(session: Any) -> dict[str, str]:
    raw = _stripe_get(session, "metadata") or {}
    if hasattr(raw, "to_dict") and callable(raw.to_dict):
        raw = raw.to_dict()
    elif not isinstance(raw, dict):
        try:
            raw = dict(raw)
        except TypeError:
            # StripeObject: use keys() / [] access
            try:
                raw = {k: raw[k] for k in raw.keys()}  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                raw = {}
    return {str(k): str(v) for k, v in raw.items() if v is not None}
