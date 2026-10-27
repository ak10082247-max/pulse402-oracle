import time
import httpx
import logging
from typing import Dict, Any, List

logger = logging.getLogger("pulse402.arb_router")

# Monitored token pairs for cross-DEX arbitrage on Base
ARB_PAIRS = [
    {
        "symbol": "WETH/USDC",
        "base_token": "0x4200000000000000000000000000000000000006",
        "quote_token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
    {
        "symbol": "AERO/USDC",
        "base_token": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
        "quote_token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    },
    {
        "symbol": "DEGEN/WETH",
        "base_token": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
        "quote_token": "0x4200000000000000000000000000000000000006",
    },
    {
        "symbol": "VIRTUAL/WETH",
        "base_token": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
        "quote_token": "0x4200000000000000000000000000000000000006",
    }
]

DEXSCREENER_URL = "https://api.dexscreener.com/latest/dex/tokens/"

async def fetch_arb_routes_state(client: httpx.AsyncClient) -> List[Dict[str, Any]]:
    """
    Scans Base cross-DEX prices across Aerodrome, Uniswap v3, and BaseSwap.
    Calculates executable smart-router paths and net profit after L2 gas and DEX fees.
    """
    results = []
    timestamp_ms = int(time.time() * 1000)
    base_l2_gas_usd = 0.015  # ~$0.015 average Base transaction gas cost

    for pair_cfg in ARB_PAIRS:
        symbol = pair_cfg["symbol"]
        base_addr = pair_cfg["base_token"]
        try:
            resp = await client.get(f"{DEXSCREENER_URL}{base_addr}", timeout=4.5)
            if resp.status_code != 200:
                continue

            pairs_data = (resp.json() or {}).get("pairs") or []
            base_pairs = [p for p in pairs_data if p.get("chainId") == "base"]
            if len(base_pairs) < 2:
                continue

            # Group by DEX
            dex_quotes = {}
            for p in base_pairs:
                dex_name = p.get("dexId", "unknown")
                price = float(p.get("priceUsd", 0) or 0)
                liq = float(p.get("liquidity", {}).get("usd", 0) or 0)
                pair_addr = p.get("pairAddress", "")
                if price > 0 and liq >= 5000:
                    if dex_name not in dex_quotes or liq > dex_quotes[dex_name]["liquidity"]:
                        dex_quotes[dex_name] = {
                            "dex": dex_name,
                            "price": price,
                            "liquidity": liq,
                            "pair_address": pair_addr
                        }

            dex_list = list(dex_quotes.values())
            if len(dex_list) < 2:
                continue

            # Find min and max price DEX
            dex_list.sort(key=lambda x: x["price"])
            buy_venue = dex_list[0]   # Lowest price (buy here)
            sell_venue = dex_list[-1]  # Highest price (sell here)

            if buy_venue["dex"] == sell_venue["dex"]:
                continue

            buy_price = buy_venue["price"]
            sell_price = sell_venue["price"]

            if buy_price <= 0:
                continue

            # Spread calculation
            spread_bps = round(((sell_price - buy_price) / buy_price) * 10000, 2)

            # Standard DEX fee is 0.30% (30 bps) on Uni/Aerodrome, round-trip fee is 60 bps
            total_fee_bps = 60.0
            net_spread_bps = round(spread_bps - total_fee_bps, 2)

            # Size test for $1,000 swap
            test_size_usd = 1000.0
            gross_profit_usd = round(test_size_usd * (spread_bps / 10000.0), 4)
            dex_fees_usd = round(test_size_usd * (total_fee_bps / 10000.0), 4)
            net_profit_usd = round(gross_profit_usd - dex_fees_usd - base_l2_gas_usd, 4)

            is_profitable = net_profit_usd > 0.05 and spread_bps > total_fee_bps

            results.append({
                "symbol": symbol,
                "route_path": f"{buy_venue['dex'].upper()} -> {sell_venue['dex'].upper()}",
                "buy_venue": {
                    "dex": buy_venue["dex"],
                    "price_usd": buy_price,
                    "liquidity_usd": buy_venue["liquidity"],
                    "pair_address": buy_venue["pair_address"]
                },
                "sell_venue": {
                    "dex": sell_venue["dex"],
                    "price_usd": sell_price,
                    "liquidity_usd": sell_venue["liquidity"],
                    "pair_address": sell_venue["pair_address"]
                },
                "spread_bps": spread_bps,
                "net_spread_bps": net_spread_bps,
                "simulation_1000_usd": {
                    "gross_profit_usd": gross_profit_usd,
                    "dex_fees_usd": dex_fees_usd,
                    "estimated_l2_gas_usd": base_l2_gas_usd,
                    "net_profit_usd": net_profit_usd,
                    "profitable": is_profitable
                },
                "execution_smart_route": {
                    "step_1": f"Swap USDC -> {symbol.split('/')[0]} on {buy_venue['dex']} ({buy_venue['pair_address']})",
                    "step_2": f"Swap {symbol.split('/')[0]} -> USDC on {sell_venue['dex']} ({sell_venue['pair_address']})",
                    "max_safe_capital_usd": min(buy_venue["liquidity"], sell_venue["liquidity"]) * 0.02
                },
                "timestamp_ms": timestamp_ms,
                "ttl_seconds": 10
            })
        except Exception as e:
            logger.warning(f"Arbitrage route scan failed for {symbol}: {e}")

    return results
