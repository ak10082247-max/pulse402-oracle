"""Deterministic background timing; no providers, payment modules or wallet inputs."""

import asyncio
import copy

import pytest

from engine import poller


def info(duration, timestamps, failed=False):
    return {
        "duration_seconds": duration,
        "acquired_timestamps_ms": timestamps,
        "failed": failed,
    }


def test_fast_refresh_retains_maximum_cadence():
    # Acquired at 100s, finished at 101s, expiry 110s. Ordinary next start at
    # 108s is already early enough to cover the measured 1s plus 0.5s margin.
    assert poller._next_poll_delay(8, 1, 101000, info(1, {"base": 100000})) == 7


def test_slower_refresh_starts_before_expiry_by_full_measured_cost():
    # Started at 100s, acquired at 101s and finished at 104s. Start next at
    # 106.5s so an equally costly acquisition finishes 0.5s before 111s expiry.
    result = poller._next_poll_delay(
        8, 4, 104000, info(4, {"base": 101000, "poly": 102000})
    )
    assert result == 2.5
    assert 104 + result + 4 == 110.5


def test_joining_existing_refresh_uses_full_cost_not_just_wait_duration():
    assert poller._next_poll_delay(8, 1, 103000, info(3, {"base": 100000})) == 3.5


def test_original_earliest_observation_drives_expiry_and_is_unchanged():
    refresh = info(3, {"base": 100000, "poly": 102000, "threat": 103000})
    original = copy.deepcopy(refresh)
    assert poller._next_poll_delay(8, 3, 103000, refresh) == 3.5
    assert refresh == original


@pytest.mark.parametrize(
    "refresh", [None, info(0.1, {}), info(0.1, {"base": 90000}, failed=True)]
)
def test_no_observations_or_failed_refresh_keeps_bounded_cadence(refresh):
    assert poller._next_poll_delay(8, 0.1, 105000, refresh) == pytest.approx(7.9)


def test_expired_successful_observation_does_not_get_a_fresh_timestamp():
    refresh = info(12, {"base": 100000})
    assert poller._next_poll_delay(8, 12, 112000, refresh) == 0.5
    assert refresh["acquired_timestamps_ms"]["base"] == 100000


def test_slow_failure_waits_at_least_half_a_second_and_period_is_capped():
    assert poller._next_poll_delay(8, 18, 120000, info(18, {}, failed=True)) == 0.5
    assert poller._next_poll_delay(60, 1, 101000, info(1, {"base": 100000})) == 7
    assert poller._next_poll_delay(5, 1, 101000, info(1, {"base": 100000})) == 4


def test_invalid_or_future_only_observation_cannot_force_a_retry_loop():
    refresh = info(1, {"bool": True, "missing": None, "future": 200000})
    assert poller._next_poll_delay(8, 1, 101000, refresh) == 7


@pytest.mark.parametrize("period", [0, -1, float("nan"), float("inf")])
def test_invalid_period_is_rejected(period):
    with pytest.raises(ValueError):
        poller._next_poll_delay(period, 0, 100000, None)


def test_timed_refresh_records_real_duration_and_only_advanced_observations(
    monkeypatch,
):
    clock = [10.0]
    monkeypatch.setattr(poller.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(poller, "_LAST_REFRESH_INFO", None)
    monkeypatch.setattr(
        poller,
        "_FEED_SNAPSHOTS",
        {"old": {"timestamp_ms": 100000}, "updated": {"timestamp_ms": 100000}},
    )

    async def refresh():
        clock[0] = 14.0
        poller._FEED_SNAPSHOTS["old"] = {"timestamp_ms": 100000}
        poller._FEED_SNAPSHOTS["updated"] = {"timestamp_ms": 101000}
        poller._FEED_SNAPSHOTS["new"] = {"timestamp_ms": 102000}

    monkeypatch.setattr(poller, "_refresh", refresh)
    asyncio.run(poller._timed_refresh())
    assert poller._LAST_REFRESH_INFO == info(4, {"updated": 101000, "new": 102000})


def test_failure_does_not_treat_retained_expired_content_as_new_acquisition(
    monkeypatch,
):
    monkeypatch.setattr(poller, "_LAST_REFRESH_INFO", None)
    monkeypatch.setattr(poller, "_FEED_SNAPSHOTS", {"old": {"timestamp_ms": 100000}})

    async def refresh():
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(poller, "_refresh", refresh)
    with pytest.raises(RuntimeError):
        asyncio.run(poller._timed_refresh())
    assert poller._LAST_REFRESH_INFO["failed"] is True
    assert poller._LAST_REFRESH_INFO["acquired_timestamps_ms"] == {}
    assert poller._FEED_SNAPSHOTS["old"]["timestamp_ms"] == 100000


def test_cancellation_preserves_full_cycle_estimate_and_propagates(monkeypatch):
    previous = info(4, {"base": 100000})
    monkeypatch.setattr(poller, "_LAST_REFRESH_INFO", previous)

    async def cancelled():
        raise asyncio.CancelledError()

    monkeypatch.setattr(poller, "_refresh", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(poller._timed_refresh())
    assert poller._LAST_REFRESH_INFO is previous


def test_background_loop_uses_expiry_schedule_and_cancels_without_extra_refresh(
    monkeypatch,
):
    calls, sleeps = [], []
    clock = {"mono": 100.0, "wall": 100.0}
    monkeypatch.setattr(poller.time, "monotonic", lambda: clock["mono"])
    monkeypatch.setattr(poller.time, "time", lambda: clock["wall"])

    async def refresh():
        calls.append(True)
        clock.update(mono=104.0, wall=104.0)
        monkeypatch.setattr(poller, "_LAST_REFRESH_INFO", info(4, {"base": 101000}))

    async def sleep(delay):
        sleeps.append(delay)
        raise asyncio.CancelledError()

    monkeypatch.setattr(poller, "_refresh_singleflight", refresh)
    monkeypatch.setattr(poller.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(poller.start_poller_loop(poll_interval_seconds=8))
    assert calls == [True] and sleeps == [2.5]
