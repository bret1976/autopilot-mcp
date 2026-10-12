from __future__ import annotations

import time
import pytest
from fastapi.testclient import TestClient

from app import gemini_budget
from app.gemini import generate_text
from app.main import app


@pytest.fixture(autouse=True)
def _fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_DAILY_BUDGET_LEDGER", str(tmp_path / "gdb.json"))
    monkeypatch.setenv("GEMINI_DAILY_BUDGET", "1")
    monkeypatch.setenv("GEMINI_DAILY_CALLS_PER_SCOPE", "3")
    monkeypatch.setenv("GEMINI_DAILY_CALLS_GLOBAL", "10")
    gemini_budget.reset_for_tests()
    yield
    gemini_budget.reset_for_tests()


def test_health_lists_pack() -> None:
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["packs"]["gemini_daily_budget"] == "gemini-daily-budget-v1"
    assert body["packs"]["run_guard"] == "run-guard-v1"
    assert body["packs"]["order_guard"] == "order-guard-v1"
    assert body["gemini_daily_budget"]["enabled"] is True
    assert body["gemini_daily_budget"]["per_scope_limit"] == 3


def test_summary_endpoint() -> None:
    gemini_budget.consume("buyer-a")
    with TestClient(app) as client:
        res = client.get("/api/gemini-daily-budget/summary")
    assert res.status_code == 200
    body = res.json()
    assert body["pack"] == "gemini-daily-budget-v1"
    assert body["global_used"] == 1
    assert body["calls_allowed"] == 1


def test_per_scope_limit_blocks() -> None:
    for _ in range(3):
        gemini_budget.consume("buyer-a")
    with pytest.raises(gemini_budget.BudgetExceeded) as exc:
        gemini_budget.consume("buyer-a")
    assert exc.value.scope == "buyer-a"
    assert exc.value.retry_after_sec >= 1
    # Other buyers still ok
    gemini_budget.consume("buyer-b")
    summary = gemini_budget.summary()
    assert summary["calls_blocked"] == 1
    assert summary["scopes_blocked"] == 1
    assert summary["global_used"] == 4


def test_global_limit_blocks_all_scopes(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_DAILY_CALLS_PER_SCOPE", "100")
    monkeypatch.setenv("GEMINI_DAILY_CALLS_GLOBAL", "2")
    gemini_budget.consume("a")
    gemini_budget.consume("b")
    with pytest.raises(gemini_budget.BudgetExceeded):
        gemini_budget.consume("c")
    assert gemini_budget.summary()["global_blocked"] == 1


def test_kill_switch_disables(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_DAILY_BUDGET", "0")
    for _ in range(20):
        gemini_budget.consume("buyer-a")
    peek = gemini_budget.peek("buyer-a")
    assert peek["enabled"] is False
    assert peek["scope_used"] == 0  # ledger not written when disabled
    assert gemini_budget.summary()["calls_allowed"] == 20
    assert gemini_budget.summary()["calls_blocked"] == 0


def test_utc_day_roll(monkeypatch, tmp_path) -> None:
    # Seed ledger for "yesterday"
    ledger = tmp_path / "gdb.json"
    monkeypatch.setenv("GEMINI_DAILY_BUDGET_LEDGER", str(ledger))
    yesterday = time.strftime(
        "%Y-%m-%d", time.gmtime(time.time() - 86400)
    )
    ledger.write_text(
        '{"day": "%s", "global": 9, "scopes": {"buyer-a": 9}}' % yesterday,
        encoding="utf-8",
    )
    peek = gemini_budget.peek("buyer-a")
    assert peek["day"] != yesterday
    assert peek["scope_used"] == 0
    assert peek["global_used"] == 0
    gemini_budget.consume("buyer-a")
    assert gemini_budget.peek("buyer-a")["scope_used"] == 1


@pytest.mark.asyncio
async def test_generate_text_respects_budget(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_DAILY_CALLS_PER_SCOPE", "1")
    calls = {"n": 0}

    async def fake_raw(api_key, model, prompt, *, grounded, timeout, budget_scope=None):
        # Bypass real budget inside fake — simulate being called after consume in real path.
        # We patch _call_model_raw AFTER consume is inside it, so we need the real consume.
        # Instead patch httpx path: leave _call_model_raw real but stub httpx.
        raise AssertionError("should not reach fake_raw directly")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            calls["n"] += 1
            return FakeResp()

    monkeypatch.setattr("app.gemini.httpx.AsyncClient", FakeClient)
    text = await generate_text("key", "hi", models=("gemini-3.6-flash",), budget_scope="buyer-z")
    assert "ok" in text
    assert calls["n"] == 1
    with pytest.raises(gemini_budget.BudgetExceeded):
        await generate_text("key", "hi again", models=("gemini-3.6-flash",), budget_scope="buyer-z")
    assert calls["n"] == 1  # second call never hit HTTP


@pytest.mark.asyncio
async def test_budget_exceeded_skips_model_failover(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_DAILY_CALLS_PER_SCOPE", "1")
    seen: list[str] = []

    class FakeResp:
        status_code = 200

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *a, **k):
            # extract model from url
            for m in ("gemini-3.1-pro-preview", "gemini-3.6-flash"):
                if m in url:
                    seen.append(m)
            return FakeResp()

    monkeypatch.setattr("app.gemini.httpx.AsyncClient", FakeClient)
    await generate_text(
        "key",
        "hi",
        models=("gemini-3.1-pro-preview", "gemini-3.6-flash"),
        budget_scope="buyer-y",
    )
    assert seen == ["gemini-3.1-pro-preview"]
    with pytest.raises(gemini_budget.BudgetExceeded):
        await generate_text(
            "key",
            "hi",
            models=("gemini-3.1-pro-preview", "gemini-3.6-flash"),
            budget_scope="buyer-y",
        )
    # Must not try flash after budget block
    assert seen == ["gemini-3.1-pro-preview"]


def test_existing_guards_still_on_health() -> None:
    with TestClient(app) as client:
        body = client.get("/health").json()
    assert body["run_guard"]["pack"] == "run-guard-v1"
    assert body["order_guard"]["pack"] == "order-guard-v1"
