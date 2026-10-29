"""Shared on-demand/background feed acquisition with original observation ages."""

import asyncio
import copy
import json
import logging
import math
import re
import time
import httpx
from .base_dex import fetch_base_liquidity_state, release_quote_cycle
from .polymarket import fetch_polymarket_velocity_state
from .threat_intel import fetch_threat_intel_state
from .arb_router import fetch_arb_routes_state
from .base_quotes import QuoteError

logger = logging.getLogger("pulse402.poller")
TTL_SECONDS = 10
FEEDS = ("base-liquidity", "polymarket-velocity", "threat-intel", "arb-routes")
STATE = {
    "base_liquidity": [],
    "polymarket_velocity": [],
    "threat_intel": {},
    "arb_routes": [],
    "last_poll_ms": 0,
    "poll_count": 0,
    "status": "initializing",
}
_FEED_SNAPSHOTS = {}
_ERRORS = {}
_ACQUISITION_METRICS = {}
_REFRESH_TASK = None
_FEED_EVENTS = {}
FEED_TIMEOUT_SECONDS = 18
REFRESH_TIMEOUT_SECONDS = 20
MAX_POLL_PERIOD_SECONDS = 8.0
MIN_POLL_DELAY_SECONDS = 0.5
REFRESH_HEADROOM_SECONDS = 0.5
_LAST_REFRESH_INFO = None
_POLL_CLIENT = None


def _safe_error_details(error):
    """Only bounded codes/statuses; exception text can contain provider secrets."""
    name = type(error).__name__
    code = name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", name) else "Exception"
    status = None
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        code = f"HTTP_{status}" if type(status) is int and 100 <= status <= 599 else code
    elif isinstance(error, QuoteError):
        if isinstance(error.code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error.code):
            code = error.code
        # QuoteError.http_status describes our response, not the provider's.
        status = getattr(error, "provider_http_status", None)
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    return code, status


def get_feed_diagnostics(feed_name):
    if feed_name not in FEEDS:
        raise ValueError("Unknown feed")
    metrics = _ACQUISITION_METRICS.get(feed_name, {})
    return {
        "last_acquisition_duration_ms": metrics.get("duration_ms"),
        "last_acquisition_completed_at_ms": metrics.get("completed_at_ms"),
        "last_acquisition_error_code": metrics.get("error_code"),
        "last_acquisition_http_status": metrics.get("http_status"),
        "last_acquisition_api_code": metrics.get("api_code"),
    }


def init_db():
    """Legacy persisted rows are not trusted as fresh acquired data."""


def _publish(feed, data):
    if isinstance(data, list):
        stamps = [row.get("timestamp_ms") for row in data if isinstance(row, dict)]
        if (
            not data
            or len(stamps) != len(data)
            or any(not isinstance(stamp, int) for stamp in stamps)
        ):
            raise ValueError("Feed has no valid acquired observations")
        timestamp = min(stamps)
    elif isinstance(data, dict) and isinstance(data.get("timestamp_ms"), int):
        timestamp = data["timestamp_ms"]
    else:
        raise ValueError("Feed has no valid acquisition timestamp")
    if timestamp > int(time.time() * 1000) + 1000:
        raise ValueError("Feed observation is in the future")
    snapshot = {
        "status": "ok",
        "feed": feed,
        "timestamp_ms": timestamp,
        "ttl_seconds": TTL_SECONDS,
        "data": copy.deepcopy(data),
    }
    _FEED_SNAPSHOTS[feed] = snapshot
    STATE[feed.replace("-", "_")] = copy.deepcopy(data)
    _ERRORS.pop(feed, None)
    if feed in _FEED_EVENTS:
        _FEED_EVENTS[feed].set()


async def run_single_poll_cycle(client):
    callbacks = (
        fetch_base_liquidity_state,
        fetch_polymarket_velocity_state,
        fetch_threat_intel_state,
        fetch_arb_routes_state,
    )

    async def acquire_one(feed, callback):
        started = time.perf_counter()
        completed, error_code, http_status, api_code = False, None, None, None
        previous_error = _ERRORS.get(feed)
        try:
            result = await asyncio.wait_for(callback(client), FEED_TIMEOUT_SECONDS)
            _publish(feed, result)
            completed = True
        except Exception as error:
            # Preserve previous content and its original timestamp; never turn a
            # failed refresh into a fresh successful response.
            error_code, http_status = _safe_error_details(error)
            value = getattr(error, "provider_api_code", None) if isinstance(error, QuoteError) else None
            if type(value) is int and -(2**31) <= value < 2**31:
                api_code = value
            _ERRORS[feed] = error_code
            completed = True
        finally:
            # A shutdown cancellation is not a completed acquisition and must
            # retain the last actual measurement rather than a partial duration.
            if completed:
                metrics = {
                    "duration_ms": max(0, round((time.perf_counter() - started) * 1000)),
                    "completed_at_ms": int(time.time() * 1000),
                    "error_code": error_code,
                    "http_status": http_status,
                    "api_code": api_code,
                }
                _ACQUISITION_METRICS[feed] = metrics
                # Log failures and recoveries at operator level. Repeated
                # successes remain available in health without noisy INFO logs.
                level = logging.WARNING if error_code else logging.INFO if previous_error else logging.DEBUG
                logger.log(level, "feed_acquisition %s", json.dumps({"feed": feed, **metrics}, sort_keys=True))
            if feed in _FEED_EVENTS:
                _FEED_EVENTS[feed].set()

    await asyncio.gather(
        *(acquire_one(feed, callback) for feed, callback in zip(FEEDS, callbacks))
    )
    STATE["last_poll_ms"] = int(time.time() * 1000)
    STATE["poll_count"] += 1
    STATE["status"] = "healthy" if not _ERRORS else "degraded"


def _feed_client():
    return httpx.AsyncClient(
        timeout=8,
        headers={"User-Agent": "Pulse402-ReadOnly-Feeds/2"},
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=64, max_keepalive_connections=64, keepalive_expiry=30),
    )


async def _run_refresh(client):
    try:
        await run_single_poll_cycle(client)
    finally:
        # Retain connections, never a previous cycle's pinned quotes or tasks.
        await release_quote_cycle(client)


async def _refresh():
    client = _POLL_CLIENT
    if client is not None and not client.is_closed:
        await _run_refresh(client)
    else:
        # Actor/standalone callers still own and close their temporary client.
        async with _feed_client() as client:
            await _run_refresh(client)


def _observation_timestamps():
    return {
        feed: snapshot["timestamp_ms"]
        for feed, snapshot in _FEED_SNAPSHOTS.items()
        if type(snapshot.get("timestamp_ms")) is int and snapshot["timestamp_ms"] > 0
    }


async def _timed_refresh():
    """Measure the whole shared task, including when a background caller joins it."""
    global _LAST_REFRESH_INFO
    before = _observation_timestamps()
    started = time.monotonic()
    completed, cancelled = False, False
    try:
        await _refresh()
        completed = True
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        # Cancellation cannot replace a full-cycle estimate with a partial one.
        if not cancelled:
            after = _observation_timestamps()
            _LAST_REFRESH_INFO = {
                "duration_seconds": max(0.0, time.monotonic() - started),
                "failed": not completed,
                "acquired_timestamps_ms": {
                    feed: stamp
                    for feed, stamp in after.items()
                    if stamp > before.get(feed, -1)
                },
            }


def _next_poll_delay(poll_interval_seconds, elapsed_seconds, now_ms, refresh_info):
    """Start early enough to cover measured acquisition, without renewing any TTL."""
    period = float(poll_interval_seconds)
    if not math.isfinite(period) or period <= 0:
        raise ValueError("Polling period must be finite and positive")
    period = max(MIN_POLL_DELAY_SECONDS, min(MAX_POLL_PERIOD_SECONDS, period))
    cadence_delay = max(MIN_POLL_DELAY_SECONDS, period - max(0.0, elapsed_seconds))
    if not refresh_info or refresh_info.get("failed"):
        return cadence_delay
    duration = refresh_info.get("duration_seconds")
    if (
        not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration < 0
    ):
        return cadence_delay
    # No progress (including a failed provider retaining old content) keeps the
    # ordinary cadence. Old expired snapshots must not trigger a tight retry loop.
    expiries = [
        stamp + TTL_SECONDS * 1000
        for stamp in refresh_info.get("acquired_timestamps_ms", {}).values()
        if type(stamp) is int and 0 < stamp <= now_ms + 1000
    ]
    if not expiries:
        return cadence_delay
    until_start = (min(expiries) - now_ms) / 1000 - duration - REFRESH_HEADROOM_SECONDS
    # When acquisition itself is too slow this cannot promise continuous
    # freshness. Retry after a bounded pause and keep the original stale age.
    return max(MIN_POLL_DELAY_SECONDS, min(cadence_delay, until_start))


async def _refresh_singleflight(feed_name=None):
    global _REFRESH_TASK, _FEED_EVENTS
    if _REFRESH_TASK is None or _REFRESH_TASK.done():
        _FEED_EVENTS = {feed: asyncio.Event() for feed in FEEDS}
        events = _FEED_EVENTS
        _REFRESH_TASK = asyncio.create_task(_timed_refresh())

        # A refresh can fail before any callback starts. Always release waiters
        # and retrieve task exceptions even if their request was cancelled.
        def settled(task):
            for event in events.values():
                event.set()
            if not task.cancelled():
                task.exception()

        _REFRESH_TASK.add_done_callback(settled)
    task = _REFRESH_TASK
    try:
        if feed_name is None:
            await asyncio.wait_for(asyncio.shield(task), REFRESH_TIMEOUT_SECONDS)
        else:
            await asyncio.wait_for(
                _FEED_EVENTS[feed_name].wait(), REFRESH_TIMEOUT_SECONDS
            )
    finally:
        if task.done() and _REFRESH_TASK is task:
            _REFRESH_TASK = None


def get_feed_state(feed_name):
    if feed_name not in FEEDS:
        raise ValueError("Unknown feed")
    snapshot = _FEED_SNAPSHOTS.get(feed_name)
    if snapshot is None:
        return {
            "status": "unavailable",
            "feed": feed_name,
            "timestamp_ms": None,
            "ttl_seconds": TTL_SECONDS,
            "data": None,
            "reason": "No acquired snapshot",
        }
    result = copy.deepcopy(snapshot)
    age = int(time.time() * 1000) - result["timestamp_ms"]
    result["age_ms"] = max(0, age)
    if age >= TTL_SECONDS * 1000:
        result["status"] = "stale"
        result["reason"] = (
            "Previous acquisition expired; no fresh timestamp was fabricated"
        )
    return result


async def acquire_feed_state(feed_name):
    state = get_feed_state(feed_name)
    if state["status"] == "ok":
        return state
    try:
        await _refresh_singleflight(feed_name)
    except Exception:
        logger.warning("Feed refresh failed")
    return get_feed_state(feed_name)


async def start_poller_loop(poll_interval_seconds=8):
    global _POLL_CLIENT, _REFRESH_TASK
    if _POLL_CLIENT is not None and not _POLL_CLIENT.is_closed:
        raise RuntimeError("A background feed client already has an owner")
    async with _feed_client() as client:
        _POLL_CLIENT = client
        try:
            while True:
                started = time.monotonic()
                try:
                    await _refresh_singleflight()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Background feed refresh failed")
                await asyncio.sleep(
                    _next_poll_delay(
                        poll_interval_seconds,
                        time.monotonic() - started,
                        int(time.time() * 1000),
                        _LAST_REFRESH_INFO,
                    )
                )
        finally:
            # Shielded foreground waiters may share this task. Drain the work
            # before the client owner closes its connection pool on shutdown.
            pending = _REFRESH_TASK
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                if _REFRESH_TASK is pending:
                    _REFRESH_TASK = None
            if _POLL_CLIENT is client:
                _POLL_CLIENT = None
