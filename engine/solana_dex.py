"""Live Solana Pump.fun & Raydium orderflow velocity and trending token discovery."""

import asyncio
import time
import httpx
from .base_quotes import QuoteError

DEXSCREENER_BOOST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q=solana%20pump"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/"


async def fetch_solana_velocity_state(client=None):
    if client is None:
        async with httpx.AsyncClient(timeout=10) as owned:
            return await fetch_solana_velocity_state(owned)

    headers = {"User-Agent": "Pulse402-Solana-Engine/1.3.0"}
    now_ms = int(time.time() * 1000)
    
    # 1. Fetch boosted trending tokens
    trending_boosts = []
    sol_boost_mints = []
    try:
        res_boost = await client.get(DEXSCREENER_BOOST_URL, headers=headers)
        if res_boost.status_code == 200:
            boosts = res_boost.json()
            if isinstance(boosts, list):
                sol_boosts = [x for x in boosts if x.get("chainId") == "solana"][:15]
                for b in sol_boosts:
                    addr = b.get("tokenAddress")
                    if addr:
                        sol_boost_mints.append(addr)
                        trending_boosts.append({
                            "mint": addr,
                            "boost_rank": b.get("amount", 0),
                            "is_pump_fun": str(addr).lower().endswith("pump"),
                            "description": b.get("description", ""),
                        })
    except Exception:
        pass

    boost_map = {b["mint"]: b["boost_rank"] for b in trending_boosts}

    # 2. Fetch live raydium and pump.fun pairs
    raw_pairs = []
    try:
        res_search = await client.get(DEXSCREENER_SEARCH_URL, headers=headers)
        if res_search.status_code == 200:
            data = res_search.json()
            raw_pairs.extend([p for p in data.get("pairs", []) if p.get("chainId") == "solana"])
    except Exception:
        pass

    # Fallback to multi-token endpoint for boosted mints if search is empty/rate-limited
    if not raw_pairs and sol_boost_mints:
        try:
            mints_query = ",".join(sol_boost_mints[:10])
            res_tokens = await client.get(DEXSCREENER_TOKENS_URL + mints_query, headers=headers)
            if res_tokens.status_code == 200:
                data = res_tokens.json()
                raw_pairs.extend([p for p in data.get("pairs", []) if p.get("chainId") == "solana"])
        except Exception:
            pass

    if not raw_pairs:
        raise QuoteError("DEXSCREENER_UNAVAILABLE", "DexScreener API returned no valid Solana pairs", 503)

    records = []
    seen_mints = set()
    for p in raw_pairs[:25]:
        base_token = p.get("baseToken", {})
        mint = base_token.get("address")
        if not mint or mint in seen_mints:
            continue
        seen_mints.add(mint)
        records.append({
            "mint": mint,
            "symbol": base_token.get("symbol"),
            "name": base_token.get("name"),
            "dex": p.get("dexId"),
            "pair_address": p.get("pairAddress"),
            "price_usd": p.get("priceUsd"),
            "price_change_5m_pct": p.get("priceChange", {}).get("m5"),
            "volume_5m_usd": p.get("volume", {}).get("m5"),
            "liquidity_usd": p.get("liquidity", {}).get("usd"),
            "fdv": p.get("fdv"),
            "is_pump_fun": str(mint).lower().endswith("pump") or p.get("dexId") == "pumpfun",
            "boost_rank": boost_map.get(mint, 0),
            "pair_created_at": p.get("pairCreatedAt"),
            "timestamp_ms": now_ms,
            "ttl_seconds": 10,
            "chain": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        })

    if not records:
        raise QuoteError("NO_SOLANA_PAIRS_ACQUIRED", "Failed to parse Solana pairs from provider", 503)

    return records
