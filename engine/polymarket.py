import time
import httpx
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger("pulse402.polymarket")

GAMMA_API_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"

# Cache rolling price memory to compute 5-minute velocity
_PRICE_HISTORY: Dict[str, List[tuple]] = {}

def update_and_get_velocity(market_id: str, current_price: float) -> float:
    now = time.time()
    if market_id not in _PRICE_HISTORY:
        _PRICE_HISTORY[market_id] = []
    
    history = _PRICE_HISTORY[market_id]
    history.append((now, current_price))
    
    # Prune older than 5 minutes (300 seconds)
    _PRICE_HISTORY[market_id] = [(t, p) for t, p in history if now - t <= 300]
    
    if len(_PRICE_HISTORY[market_id]) < 2:
        return 0.0
    
    oldest_price = _PRICE_HISTORY[market_id][0][1]
    if oldest_price <= 0:
        return 0.0
    
    velocity_pct = round(((current_price - oldest_price) / oldest_price) * 100.0, 2)
    return velocity_pct

async def fetch_polymarket_velocity_state(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """
    Polls active Polymarket prediction markets for probability velocity (>2% in 5 min)
    and orderbook spread imbalances.
    """
    results = []
    timestamp_ms = int(time.time() * 1000)

    try:
        params = {
            "active": "true",
            "closed": "false",
            "limit": 20,
            "order": "volume24hr",
            "ascending": "false",
        }
        resp = await client.get(GAMMA_API_URL, params=params, timeout=5.0)
        if resp.status_code != 200:
            return results

        raw_markets = resp.json()
        if not isinstance(raw_markets, list):
            return results

        for m in raw_markets[:15]:
            market_id = str(m.get("id") or "")
            question = str(m.get("question") or "Unknown Market")
            slug = str(m.get("slug") or "")
            volume_24h = float(m.get("volume24hr") or m.get("volumeNum") or 0.0)
            liquidity = float(m.get("liquidity") or m.get("liquidityNum") or 0.0)
            outcome_prices = m.get("outcomePrices") or ["0.5", "0.5"]

            # Parse probability of YES outcome
            yes_prob = 0.5
            try:
                if isinstance(outcome_prices, str):
                    import json
                    outcome_prices = json.loads(outcome_prices)
                if isinstance(outcome_prices, list) and len(outcome_prices) > 0:
                    yes_prob = float(outcome_prices[0])
            except Exception:
                pass

            # Calculate 5-minute velocity
            velocity_5m_pct = update_and_get_velocity(market_id, yes_prob)

            # Orderbook depth and spread analysis
            clob_token_ids = m.get("clobTokenIds") or []
            if isinstance(clob_token_ids, str):
                import json
                try:
                    clob_token_ids = json.loads(clob_token_ids)
                except Exception:
                    clob_token_ids = []

            spread_bps = 50.0  # default 0.5% spread
            imbalance_ratio = 0.0
            bid_depth_usd = round(liquidity * 0.45, 2)
            ask_depth_usd = round(liquidity * 0.55, 2)

            if clob_token_ids and isinstance(clob_token_ids, list):
                token_id = str(clob_token_ids[0])
                try:
                    book_resp = await client.get(f"{CLOB_BOOK_URL}?token_id={token_id}", timeout=2.0)
                    if book_resp.status_code == 200:
                        book_data = book_resp.json()
                        bids = book_data.get("bids") or []
                        asks = book_data.get("asks") or []

                        if bids and asks:
                            best_bid = float(bids[0].get("price", 0) or 0)
                            best_ask = float(asks[0].get("price", 0) or 0)
                            if best_ask > best_bid > 0:
                                spread_bps = round(((best_ask - best_bid) / best_bid) * 10000, 1)

                            total_bid_size = sum(float(b.get("size", 0) or 0) for b in bids[:5])
                            total_ask_size = sum(float(a.get("size", 0) or 0) for a in asks[:5])
                            if total_bid_size + total_ask_size > 0:
                                imbalance_ratio = round((total_bid_size - total_ask_size) / (total_bid_size + total_ask_size), 3)
                except Exception:
                    pass

            # Determine high-conviction signal
            is_high_velocity = abs(velocity_5m_pct) >= 2.0
            signal_type = "NEUTRAL"
            if velocity_5m_pct >= 2.0 or imbalance_ratio >= 0.35:
                signal_type = "BULLISH_WHALE_ACCUMULATION"
            elif velocity_5m_pct <= -2.0 or imbalance_ratio <= -0.35:
                signal_type = "BEARISH_LIQUIDATION_PRESSURE"

            results.append({
                "market_id": market_id,
                "question": question,
                "slug": slug,
                "current_probability": round(yes_prob, 4),
                "velocity_5m_pct": velocity_5m_pct,
                "high_velocity_alert": is_high_velocity,
                "signal_type": signal_type,
                "orderbook": {
                    "spread_bps": spread_bps,
                    "imbalance_ratio": imbalance_ratio,
                    "estimated_bid_depth_usd": bid_depth_usd,
                    "estimated_ask_depth_usd": ask_depth_usd
                },
                "volume_24h_usd": volume_24h,
                "liquidity_usd": liquidity,
                "timestamp_ms": timestamp_ms,
                "ttl_seconds": 10
            })
    except Exception as e:
        logger.warning(f"Polymarket velocity scan failed: {e}")

    return results
