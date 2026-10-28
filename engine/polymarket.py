"""Measured Polymarket price histories and actual sorted CLOB book depth."""

import asyncio
import hashlib
import json
import time
from decimal import Decimal, InvalidOperation

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
HISTORY_URL = "https://clob.polymarket.com/prices-history"


def numeric(value, *, probability=False):
    try:
        number = Decimal(str(value))
        if (
            not number.is_finite()
            or number < 0
            or number > Decimal("1e30")
            or (probability and number > 1)
        ):
            return None
        return number
    except (InvalidOperation, ValueError, TypeError):
        return None


def book_metrics(book, token, maximum_source_age_ms=None):
    if not isinstance(book, dict) or str(book.get("asset_id")) != token:
        raise ValueError("Book token mismatch")
    timestamp = numeric(book.get("timestamp"))
    if timestamp is None or timestamp <= 0 or timestamp != int(timestamp):
        raise ValueError("Book has no valid source timestamp")
    source_age_ms = int(time.time() * 1000) - int(timestamp)
    if source_age_ms < -5000 or (
        maximum_source_age_ms is not None and source_age_ms > maximum_source_age_ms
    ):
        raise ValueError("Book source timestamp is outside the allowed age")

    def levels(side, reverse):
        rows = []
        for row in book.get(side, []):
            if not isinstance(row, dict):
                continue
            price, size = numeric(row.get("price"), probability=True), numeric(
                row.get("size")
            )
            if price is not None and price > 0 and size is not None and size > 0:
                rows.append((price, size))
        return sorted(rows, reverse=reverse)[:200]

    bids, asks = levels("bids", True), levels("asks", False)
    bid, ask = (bids[0][0] if bids else None), (asks[0][0] if asks else None)
    bid_depth = sum((price * size for price, size in bids), Decimal(0))
    ask_depth = sum((price * size for price, size in asks), Decimal(0))
    spread = float((ask - bid) / bid * 10000) if bid and ask and ask >= bid else None
    return {
        "best_bid": float(bid) if bid is not None else None,
        "best_ask": float(ask) if ask is not None else None,
        "midpoint_probability": (
            float((bid + ask) / 2) if bid and ask and ask >= bid else None
        ),
        "spread_bps": spread,
        "imbalance_ratio": (
            float((bid_depth - ask_depth) / (bid_depth + ask_depth))
            if bid_depth + ask_depth
            else None
        ),
        "bid_depth_usdc": str(bid_depth),
        "ask_depth_usdc": str(ask_depth),
        "levels_included": {"bids": len(bids), "asks": len(asks)},
        "depth_scope": "Up to 200 sorted levels per side; sum(price*shares), not TVL allocation",
        "source_timestamp_ms": int(timestamp),
        "source_age_at_acquisition_ms": source_age_ms,
        "maximum_source_age_ms": maximum_source_age_ms,
        "source_time_scope": "Exchange book state timestamp; HTTP retrieval does not imply a new trade or book change",
        "book_sha256": hashlib.sha256(
            json.dumps(book, sort_keys=True).encode()
        ).hexdigest(),
    }


def history_velocity(history, now=None):
    now = time.time() if now is None else now
    points = {}
    for point in history if isinstance(history, list) else []:
        if not isinstance(point, dict):
            continue
        timestamp = numeric(point.get("t"))
        price = numeric(point.get("p"), probability=True)
        if (
            timestamp is not None
            and price is not None
            and 0 < float(timestamp) <= now + 15
        ):
            points[int(timestamp)] = price
    if len(points) < 2:
        return {
            "status": "insufficient_history",
            "velocity_5m_pct": None,
            "window_seconds": None,
        }
    end = max(points)
    target = end - 300
    start = min(points, key=lambda timestamp: abs(timestamp - target))
    elapsed = end - start
    if not 240 <= elapsed <= 420 or now - end > 180 or points[start] <= 0:
        return {
            "status": "insufficient_recent_5m_coverage",
            "velocity_5m_pct": None,
            "window_seconds": elapsed,
            "last_sample_timestamp": end,
        }
    return {
        "status": "measured",
        "velocity_5m_pct": float((points[end] - points[start]) / points[start] * 100),
        "probability_change_percentage_points": float(
            (points[end] - points[start]) * 100
        ),
        "window_seconds": elapsed,
        "first_timestamp": start,
        "last_timestamp": end,
        "first_probability": str(points[start]),
        "last_probability": str(points[end]),
        "method": "CLOB historical price observations, not inferred flow or wallet identity",
    }


async def fetch_polymarket_velocity_state(client):
    response = await client.get(
        GAMMA_API_URL,
        params={
            "active": "true",
            "closed": "false",
            "limit": 12,
            "order": "volume24hr",
            "ascending": "false",
        },
        timeout=7,
    )
    response.raise_for_status()
    discovery_acquired_at_ms = int(time.time() * 1000)
    markets = response.json()
    if not isinstance(markets, list):
        raise ValueError("Invalid market discovery")
    budget = asyncio.Semaphore(6)

    async def observed_get(url, params):
        response = await client.get(url, params=params, timeout=5)
        return response, int(time.time() * 1000)

    async def market(row):
        if not isinstance(row, dict) or row.get("closed") is True:
            return None
        try:
            tokens = row.get("clobTokenIds") or []
            tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
            outcomes = row.get("outcomes") or []
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
            if (
                not isinstance(tokens, list)
                or not tokens
                or not str(tokens[0]).isdigit()
            ):
                return None
            token = str(tokens[0])
            async with budget:
                (book_response, book_acquired_at_ms), (
                    history_response,
                    history_acquired_at_ms,
                ) = await asyncio.gather(
                    observed_get(CLOB_BOOK_URL, {"token_id": token}),
                    observed_get(
                        HISTORY_URL,
                        {"market": token, "interval": "1h", "fidelity": 1},
                    ),
                )
            book_response.raise_for_status()
            book = book_metrics(
                book_response.json(), token, maximum_source_age_ms=30000
            )
            history = (
                history_velocity(history_response.json().get("history", []))
                if history_response.status_code == 200
                else history_velocity([])
            )
            velocity = history["velocity_5m_pct"]
            current = book["midpoint_probability"]
            return {
                "market_id": str(row.get("id") or ""),
                "question": str(row.get("question") or ""),
                "slug": str(row.get("slug") or ""),
                "outcome": outcomes[0] if outcomes else "first indexed outcome",
                "token_id": token,
                "current_probability": float(current) if current is not None else None,
                "current_probability_basis": (
                    "displayed_orderbook_midpoint"
                    if current is not None
                    else "unavailable_two_sided_noncrossed_book_required"
                ),
                "current_probability_source_timestamp_ms": book["source_timestamp_ms"],
                "velocity_5m_pct": velocity,
                "velocity_evidence": history,
                "high_velocity_alert": (
                    abs(velocity) >= 2 if velocity is not None else None
                ),
                "signal_type": (
                    "MEASURED_PRICE_INCREASE"
                    if velocity is not None and velocity >= 2
                    else (
                        "MEASURED_PRICE_DECREASE"
                        if velocity is not None and velocity <= -2
                        else (
                            "NO_THRESHOLD_CROSSING"
                            if velocity is not None
                            else "INSUFFICIENT_HISTORY"
                        )
                    )
                ),
                "whale_identity": None,
                "orderbook": book,
                "volume_24h_usd": (
                    float(numeric(row["volume24hr"]))
                    if numeric(row.get("volume24hr")) is not None
                    else None
                ),
                "liquidity_usd": (
                    float(numeric(row.get("liquidityNum", row.get("liquidity"))))
                    if numeric(row.get("liquidityNum", row.get("liquidity")))
                    is not None
                    else None
                ),
                "timestamp_ms": min(
                    discovery_acquired_at_ms,
                    book_acquired_at_ms,
                    history_acquired_at_ms,
                ),
                "ttl_seconds": 10,
                "evidence": {
                    "providers": ["Polymarket Gamma", "Polymarket CLOB"],
                    "maximum_history_sample_age_seconds": 180,
                    "scope": "Price movement and displayed depth do not identify smart money, whale accumulation or executed netflow",
                },
            }
        except Exception:
            return None

    rows = await asyncio.gather(*(market(row) for row in markets[:12]))
    return [row for row in rows if row]
