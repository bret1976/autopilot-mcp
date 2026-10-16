"""growth-metrics-fix-v1: PostProxy metrics calls use /api paths, stop on auth errors."""
import asyncio
from datetime import timedelta

import pytest

from app import growth_v2, postproxy


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("GROWTH_METRICS_FIX", raising=False)


def test_paths_have_api_prefix():
    assert growth_v2.profile_detail_path("p1") == "/api/profiles/p1"
    assert growth_v2._api_path(growth_v2.POST_STATS_PATH) == "/api/posts/stats"
    assert growth_v2._api_path("/api/profiles") == "/api/profiles"


def test_kill_switch_restores_old_paths(monkeypatch):
    monkeypatch.setenv("GROWTH_METRICS_FIX", "0")
    assert growth_v2.profile_detail_path("p1") == "/profiles/p1"
    assert growth_v2._api_path("/posts/stats") == "/posts/stats"
    assert growth_v2.metrics_fix_summary()["enabled"] is False


def _fake_profiles(monkeypatch, detail):
    calls = []

    async def list_profiles(key, group=""):
        return {"data": [{"id": "a", "platform": "instagram"}, {"id": "b", "platform": "youtube"}]}

    async def request(method, path, key, **kw):
        calls.append(path)
        return detail(path)

    monkeypatch.setattr(postproxy, "list_profiles", list_profiles)
    monkeypatch.setattr(postproxy, "_request", request)
    return calls


def test_snapshot_reads_followers_from_api_path(monkeypatch):
    def detail(path):
        assert path.startswith("/api/profiles/")
        return {"latest_stats": [{"placement_id": None, "stats": {"followers_count": 42}, "recorded_at": "x"}]}

    calls = _fake_profiles(monkeypatch, detail)
    state = {}
    snap = _run(growth_v2.snapshot_followers({"postproxy_api_key": "k"}, state))
    assert calls == ["/api/profiles/a", "/api/profiles/b"]
    assert snap["by_platform"]["instagram"]["followers"] == 42
    assert state["follower_history"][-1]["youtube"] == 42


def test_auth_error_stops_after_first_profile(monkeypatch):
    def detail(path):
        raise postproxy.PostProxyError('PostProxy 401: {"error":"Invalid API key"}')

    calls = _fake_profiles(monkeypatch, detail)
    before = growth_v2.metrics_fix_summary()["since_boot"]["auth_short_circuits"]
    _run(growth_v2.snapshot_followers({"postproxy_api_key": "k"}, {}))
    assert len(calls) == 1
    summ = growth_v2.metrics_fix_summary()["since_boot"]
    assert summ["auth_short_circuits"] == before + 1
    assert summ["followers"]["last_error"] == "PostProxy 401"
    assert "Invalid" not in str(summ)  # never the body


def test_other_errors_keep_going(monkeypatch):
    def detail(path):
        raise postproxy.PostProxyError("PostProxy 500: boom")

    calls = _fake_profiles(monkeypatch, detail)
    _run(growth_v2.snapshot_followers({"postproxy_api_key": "k"}, {}))
    assert len(calls) == 2


def test_empty_previous_snapshot_does_not_hold_gate(monkeypatch):
    calls = _fake_profiles(monkeypatch, lambda p: {"latest_stats": []})
    recent = (growth_v2._utcnow() - timedelta(hours=1)).isoformat()
    _run(growth_v2.snapshot_followers({"postproxy_api_key": "k"}, {"followers": {"at": recent, "by_platform": {}}}))
    assert len(calls) == 2


def test_real_previous_snapshot_holds_gate(monkeypatch):
    calls = _fake_profiles(monkeypatch, lambda p: {"latest_stats": []})
    recent = (growth_v2._utcnow() - timedelta(hours=1)).isoformat()
    state = {"followers": {"at": recent, "by_platform": {"instagram": {"followers": 1}}}}
    assert _run(growth_v2.snapshot_followers({"postproxy_api_key": "k"}, state)) is None
    assert calls == []


def test_kill_switch_keeps_old_gate(monkeypatch):
    monkeypatch.setenv("GROWTH_METRICS_FIX", "0")
    calls = _fake_profiles(monkeypatch, lambda p: {"latest_stats": []})
    recent = (growth_v2._utcnow() - timedelta(hours=1)).isoformat()
    assert _run(growth_v2.snapshot_followers({"postproxy_api_key": "k"}, {"followers": {"at": recent, "by_platform": {}}})) is None
    assert calls == []


def test_post_stats_uses_api_path_and_stops_on_401(monkeypatch):
    seen = []

    async def request(method, path, key, **kw):
        seen.append(path)
        raise postproxy.PostProxyError("PostProxy 401: nope")

    monkeypatch.setattr(postproxy, "_request", request)
    ids = [f"p{i}" for i in range(120)]  # 3 chunks of 50
    assert _run(growth_v2.fetch_post_stats("k", ids)) == {}
    assert seen == ["/api/posts/stats"]
