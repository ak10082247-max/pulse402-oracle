"""Background pacing of rate-limited providers never starves paid acquisitions."""
import asyncio
import copy

import pytest

from engine import poller

CALLBACKS = (
    ("base-liquidity", "fetch_base_liquidity_state"),
    ("polymarket-velocity", "fetch_polymarket_velocity_state"),
    ("threat-intel", "fetch_threat_intel_state"),
    ("arb-routes", "fetch_arb_routes_state"),
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(poller, "_FEED_SNAPSHOTS", {})
    monkeypatch.setattr(poller, "_FEED_EVENTS", {})
    monkeypatch.setattr(poller, "_ERRORS", {})
    monkeypatch.setattr(poller, "_ACQUISITION_METRICS", {})
    monkeypatch.setattr(poller, "_LAST_ATTEMPT_MONOTONIC", {})
    monkeypatch.setattr(poller, "_REFRESH_TASK", None)
    monkeypatch.setattr(poller, "_REFRESH_SKIPPED", frozenset())
    monkeypatch.setattr(poller, "_POLL_CLIENT", None)
    monkeypatch.setattr(poller, "STATE", copy.deepcopy(poller.STATE))
    monkeypatch.setattr(poller, "release_quote_cycle", _noop_release)


async def _noop_release(_client):
    return None


def install(monkeypatch, calls):
    for feed, name in CALLBACKS:
        async def callback(_client, feed=feed):
            calls.append(feed)
            return [{"timestamp_ms": int(poller.time.time() * 1000)}]
        monkeypatch.setattr(poller, name, callback)


class _Client:
    is_closed = False


def test_background_cycle_paces_threat_intel_but_foreground_always_acquires(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(poller.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(poller, "_POLL_CLIENT", _Client())
    calls = []
    install(monkeypatch, calls)

    async def background():
        token = poller._BACKGROUND.set(True)
        try:
            await poller._refresh_singleflight()
        finally:
            poller._BACKGROUND.reset(token)

    async def run():
        await background()                       # first cycle: everything
        assert sorted(calls) == sorted(f for f, _ in CALLBACKS)
        calls.clear(); clock[0] += 8
        await background()                       # 8s later: GoPlus paced
        assert "threat-intel" not in calls and len(calls) == 3
        calls.clear(); clock[0] += 1
        await poller._refresh_singleflight()     # foreground: never paced
        assert "threat-intel" in calls and len(calls) == 4
        calls.clear(); clock[0] += poller.BACKGROUND_MIN_INTERVAL_SECONDS["threat-intel"]
        await background()                       # interval elapsed: GoPlus again
        assert "threat-intel" in calls
    asyncio.run(run())


def test_paid_caller_joining_a_paced_background_cycle_still_gets_a_fresh_acquisition(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(poller.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(poller, "_POLL_CLIENT", _Client())
    calls = []
    install(monkeypatch, calls)
    poller._LAST_ATTEMPT_MONOTONIC["threat-intel"] = clock[0] - 1  # recently polled

    async def run():
        token = poller._BACKGROUND.set(True)
        try:
            background = asyncio.create_task(poller._refresh_singleflight())
        finally:
            poller._BACKGROUND.reset(token)
        await asyncio.sleep(0)
        assert "threat-intel" in poller._REFRESH_SKIPPED
        await poller._refresh_singleflight("threat-intel")   # joins, then re-acquires
        await background
        assert calls.count("threat-intel") == 1
        assert poller.get_feed_state("threat-intel")["status"] == "ok"
    asyncio.run(run())


def test_health_treats_successful_paced_feed_as_healthy_without_renewing_its_timestamp(monkeypatch):
    import app
    monkeypatch.setattr(poller.time, "time", lambda: 100.0)
    for feed, _ in CALLBACKS:
        stamp = 80000 if feed == "threat-intel" else 99000
        poller._publish(feed, [{"timestamp_ms": stamp}])
        poller._ACQUISITION_METRICS[feed] = {"duration_ms": 200, "completed_at_ms": stamp, "error_code": None, "http_status": None, "api_code": None}
    health = asyncio.run(app.health_check())
    assert health["engine_status"] == "healthy"
    assert health["feed_status"]["threat-intel"]["status"] == "stale"
    assert health["feed_status"]["threat-intel"]["observed_at_ms"] == 80000
    assert health["feed_status"]["threat-intel"]["refresh_mode"] == "on_demand_paced"
    preview = app.preview_payload("threat-intel")
    assert preview["data"]["feed_status"] == "ready_on_demand" and preview["data"]["observed_at_ms"] == 80000

    poller._ERRORS["threat-intel"] = "GOPLUS_API_REJECTED"
    poller._ACQUISITION_METRICS["threat-intel"]["error_code"] = "GOPLUS_API_REJECTED"
    assert asyncio.run(app.health_check())["engine_status"] == "degraded"
    assert app.preview_payload("threat-intel")["data"]["feed_status"] == "stale"


def test_non_paced_stale_feed_still_degrades_health(monkeypatch):
    import app
    monkeypatch.setattr(poller.time, "time", lambda: 100.0)
    for feed, _ in CALLBACKS:
        stamp = 80000 if feed == "base-liquidity" else 99000
        poller._publish(feed, [{"timestamp_ms": stamp}])
    assert asyncio.run(app.health_check())["engine_status"] == "degraded"
