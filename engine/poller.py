"""Shared on-demand/background feed acquisition with original observation ages."""

import asyncio
import copy
import logging
import time
import httpx
from .base_dex import fetch_base_liquidity_state, release_quote_cycle
from .polymarket import fetch_polymarket_velocity_state
from .threat_intel import fetch_threat_intel_state
from .arb_router import fetch_arb_routes_state

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
_REFRESH_TASK = None
_FEED_EVENTS = {}
FEED_TIMEOUT_SECONDS = 18
REFRESH_TIMEOUT_SECONDS = 20


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
        try:
            result = await asyncio.wait_for(callback(client), FEED_TIMEOUT_SECONDS)
            _publish(feed, result)
        except Exception as error:
            # Preserve previous content and its original timestamp; never turn a
            # failed refresh into a fresh successful response.
            _ERRORS[feed] = type(error).__name__
        finally:
            if feed in _FEED_EVENTS:
                _FEED_EVENTS[feed].set()

    await asyncio.gather(
        *(acquire_one(feed, callback) for feed, callback in zip(FEEDS, callbacks))
    )
    STATE["last_poll_ms"] = int(time.time() * 1000)
    STATE["poll_count"] += 1
    STATE["status"] = "healthy" if not _ERRORS else "degraded"


async def _refresh():
    async with httpx.AsyncClient(
        timeout=8,
        headers={"User-Agent": "Pulse402-ReadOnly-Feeds/2"},
        follow_redirects=False,
        trust_env=False,
    ) as client:
        try:
            await run_single_poll_cycle(client)
        finally:
            await release_quote_cycle(client)


async def _refresh_singleflight(feed_name=None):
    global _REFRESH_TASK, _FEED_EVENTS
    if _REFRESH_TASK is None or _REFRESH_TASK.done():
        _FEED_EVENTS = {feed: asyncio.Event() for feed in FEEDS}
        events = _FEED_EVENTS
        _REFRESH_TASK = asyncio.create_task(_refresh())

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


async def start_poller_loop(poll_interval_seconds=10):
    while True:
        started = time.monotonic()
        try:
            await _refresh_singleflight()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Background feed refresh failed")
        await asyncio.sleep(
            max(0.5, poll_interval_seconds - (time.monotonic() - started))
        )
