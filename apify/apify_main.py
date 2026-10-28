"""Deliver original local provider observations through Apify's own PPE channel."""
import asyncio
from decimal import Decimal
import inspect
import json
import time

from apify import Actor

from engine import poller

EVENT_NAME = "oracle-feed-fetch"
FEEDS = {
    "base_liquidity": "base-liquidity",
    "polymarket_velocity": "polymarket-velocity",
    "threat_intel": "threat-intel",
    "arb_routes": "arb-routes",
}


def selected_feeds(actor_input):
    if actor_input is None:
        actor_input = {}
    if not isinstance(actor_input, dict) or set(actor_input) - {"feed"}:
        raise ValueError("Input must be an object containing only the feed selector")
    choice = actor_input.get("feed", "all")
    if not isinstance(choice, str) or choice not in ("all", *FEEDS):
        raise ValueError("Choose all, base_liquidity, polymarket_velocity, threat_intel or arb_routes")
    return list(FEEDS.values()) if choice == "all" else [FEEDS[choice]]


def validated_snapshot(snapshot, feed):
    """Keep source timestamps intact; summaries, stale/error/empty data are not deliveries."""
    if not isinstance(snapshot, dict) or snapshot.get("status") != "ok" or snapshot.get("feed") != feed or snapshot.get("error"):
        raise ValueError("Feed did not provide a successful acquired snapshot")
    stamp, ttl = snapshot.get("timestamp_ms"), snapshot.get("ttl_seconds")
    if type(stamp) is not int or stamp <= 0 or type(ttl) is not int or ttl <= 0:
        raise ValueError("Feed has no valid original observation time")
    age = int(time.time() * 1000) - stamp
    if age < -1000 or age >= min(ttl, poller.TTL_SECONDS) * 1000:
        raise ValueError("Feed observation is stale or future dated")
    data = snapshot.get("data")
    rows = data.get("monitored_token_assessments") if feed == "threat-intel" and isinstance(data, dict) else data
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) or not row or row.get("error") or row.get("status") in ("error", "unavailable", "stale", "preview") for row in rows):
        raise ValueError("Feed contains no complete acquired records")
    if isinstance(data, dict) and (data.get("error") or data.get("status") in ("error", "unavailable", "stale", "preview")):
        raise ValueError("Feed contains an unsuccessful result")
    encoded = json.dumps(snapshot, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 2_000_000:
        raise ValueError("Feed exceeds the delivery size limit")
    # A detached copy prevents a dataset adapter from mutating shared feed state.
    return json.loads(encoded)


def charging_manager():
    if "charged_event_name" not in inspect.signature(Actor.push_data).parameters:
        raise RuntimeError("Apify SDK with charged push_data support is required (verified in 2.7 and 4.1)")
    manager = Actor.get_charging_manager()
    pricing = manager.get_pricing_info()
    price = Decimal(str(pricing.per_event_prices.get(EVENT_NAME, 0)))
    if pricing.is_pay_per_event is not True or not price.is_finite() or price <= 0:
        raise RuntimeError("Configure a positive oracle-feed-fetch pay-per-event price in Apify Console")
    return manager


async def acquire(feed):
    try:
        snapshot = await asyncio.wait_for(poller.acquire_feed_state(feed), timeout=poller.REFRESH_TIMEOUT_SECONDS + 1)
        return feed, validated_snapshot(snapshot, feed)
    except Exception as error:
        Actor.log.warning("No delivery or PPE charge for %s: %s", feed, type(error).__name__)
        return feed, None


async def main():
    async with Actor:
        feeds = selected_feeds(await Actor.get_input())
        manager = charging_manager()
        remaining = manager.calculate_max_event_charge_count_within_limit(EVENT_NAME)
        if remaining is not None:
            if type(remaining) is not int:
                raise RuntimeError("Invalid Apify event budget")
            if remaining <= 0:
                Actor.log.info("No remaining PPE budget; no feed acquisition or delivery started")
                return
        tasks = [asyncio.create_task(acquire(feed)) for feed in feeds]
        try:
            for ready in asyncio.as_completed(tasks):
                feed, snapshot = await ready
                if snapshot is None:
                    continue
                try:
                    snapshot = validated_snapshot(snapshot, feed)
                except ValueError:
                    Actor.log.warning("No delivery or PPE charge for expired queued feed %s", feed)
                    continue
                # Official SDK shortcut stores this one full-feed item before
                # charging one event, and omits writes beyond the user's budget.
                # Do not separately call Actor.charge or retry an ambiguous push.
                result = await Actor.push_data(snapshot, charged_event_name=EVENT_NAME)
                if result.charged_count == 1:
                    Actor.log.info("Delivered original %s observations and charged one PPE event", feed)
                elif not (result.charged_count == 0 and result.event_charge_limit_reached):
                    raise RuntimeError("Apify did not confirm the expected delivery event")
                if result.event_charge_limit_reached:
                    break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
