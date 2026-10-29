"""Original observation freshness is checked before a paid delivery starts."""
import asyncio
import copy

import pytest

import app as web
from engine import payments, poller


def test_near_expiry_snapshot_refreshes_before_paid_acquisition(monkeypatch):
    monkeypatch.setattr(poller.time, "time", lambda: 100.0)
    monkeypatch.setattr(poller, "_FEED_SNAPSHOTS", {})
    poller._publish("base-liquidity", [{"timestamp_ms": 91000}])
    refreshed = []

    async def refresh(name):
        refreshed.append(name)
        poller._publish(name, [{"timestamp_ms": 99000}])

    monkeypatch.setattr(poller, "_refresh_singleflight", refresh)
    result = asyncio.run(poller.acquire_feed_state("base-liquidity", min_remaining_ms=4000))
    assert refreshed == ["base-liquidity"]
    assert result["timestamp_ms"] == 99000 and result["age_ms"] == 1000


def test_failed_headroom_refresh_does_not_renew_or_relabel_health_observation(monkeypatch):
    monkeypatch.setattr(poller.time, "time", lambda: 100.0)
    monkeypatch.setattr(poller, "_FEED_SNAPSHOTS", {})
    poller._publish("base-liquidity", [{"timestamp_ms": 91000}])

    async def refresh(_name):
        raise RuntimeError("offline provider failure")

    monkeypatch.setattr(poller, "_refresh_singleflight", refresh)
    delivery = asyncio.run(poller.acquire_feed_state("base-liquidity", min_remaining_ms=4000))
    health = poller.get_feed_state("base-liquidity")
    assert delivery["status"] == "unavailable" and delivery["timestamp_ms"] == 91000
    assert health["status"] == "ok" and health["timestamp_ms"] == 91000


def test_paid_acquisition_preserves_valid_original_timestamp_and_avoids_refresh(monkeypatch):
    monkeypatch.setattr(poller.time, "time", lambda: 100.0)
    monkeypatch.setattr(poller, "_FEED_SNAPSHOTS", {})
    poller._publish("base-liquidity", [{"timestamp_ms": 98000}])

    async def forbidden(_name):
        raise AssertionError("Already acquired data has sufficient original freshness")

    monkeypatch.setattr(poller, "_refresh_singleflight", forbidden)
    assert asyncio.run(poller.acquire_feed_state("base-liquidity", min_remaining_ms=4000))["timestamp_ms"] == 98000


@pytest.mark.parametrize("headroom", [-1, 10000, True, 4.0, "4000"])
def test_invalid_headroom_is_rejected(monkeypatch, headroom):
    with pytest.raises(ValueError):
        asyncio.run(poller.acquire_feed_state("base-liquidity", min_remaining_ms=headroom))


def test_requested_missing_symbol_is_not_a_chargeable_empty_success(monkeypatch):
    source = {"status": "ok", "timestamp_ms": 99000, "ttl_seconds": 10, "data": [{"symbol": "AERO"}]}
    original = copy.deepcopy(source)

    async def acquire(name, *, min_remaining_ms):
        assert name == "base-liquidity" and min_remaining_ms == 4000
        return source

    monkeypatch.setattr(poller, "acquire_feed_state", acquire)
    with pytest.raises(payments.PaymentError) as error:
        asyncio.run(web.acquire_feed("base-liquidity", {"symbol": "WETH"}))
    assert error.value.code == "REQUESTED_SYMBOL_UNAVAILABLE" and error.value.charged is False
    assert source == original


@pytest.mark.parametrize("timestamp,valid", [(99000, True), (91000, True), (90999, False), (100999, True), (101001, False), (True, False), (None, False)])
def test_delivery_guard_uses_original_timestamp_and_one_second_headroom(monkeypatch, timestamp, valid):
    monkeypatch.setattr(web.time, "time", lambda: 100.0)
    assert web.feed_delivery_valid({"status": "ok", "ttl_seconds": 10, "timestamp_ms": timestamp}) is valid
