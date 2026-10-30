"""Live Solana Pump.fun & Raydium orderflow velocity and trending token discovery."""

import asyncio
import time
import httpx

DEXSCREENER_BOOST_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search?q=solana%20pump"


async def fetch_solana_velocity_state(client=None):
    if client is None:
        async with httpx.AsyncClient(timeout=10) as owned:
            return await fetch_solana_velocity_state(owned)

    headers = {"User-Agent": "Pulse402-Solana-Engine/1.3.0"}
    
    # 1. Fetch boosted trending tokens
    trending_boosts = []
    try:
        res_boost = await client.get(DEXSCREENER_BOOST_URL, headers=headers)
        if res_boost.status_code == 200:
            boosts = res_boost.json()
            if isinstance(boosts, list):
                sol_boosts = [x for x in boosts if x.get("chainId") == "solana"][:15]
                for b in sol_boosts:
                    addr = b.get("tokenAddress")
                    if addr:
                        trending_boosts.append({
                            "mint": addr,
                            "boost_rank": b.get("amount", 0),
                            "is_pump_fun": str(addr).lower().endswith("pump"),
                            "description": b.get("description", ""),
                        })
    except Exception:
        pass

    # 2. Fetch live raydium and pump.fun pairs
    records = []
    now_ms = int(time.time() * 1000)
    boost_map = {b["mint"]: b["boost_rank"] for b in trending_boosts}

    try:
        res_search = await client.get(DEXSCREENER_SEARCH_URL, headers=headers)
        if res_search.status_code == 200:
            data = res_search.json()
            pairs = [p for p in data.get("pairs", []) if p.get("chainId") == "solana"]
            for p in pairs[:25]:
                base_token = p.get("baseToken", {})
                mint = base_token.get("address")
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
    except Exception:
        pass

    return records
