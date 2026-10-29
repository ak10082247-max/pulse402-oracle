"""Exact-amount read-only round trips; no fabricated gas, net profit or execution."""

import asyncio
from decimal import Decimal
from .base_dex import TOKENS, acquire_quote_cycle, quote_freshness, gather_quote_reads
from .base_quotes import QuoteError

ARB_PAIRS = [
    ("WETH", "USDC", "1"),
    ("AERO", "USDC", "1000"),
    ("DEGEN", "WETH", "10000"),
    ("VIRTUAL", "WETH", "100"),
]


async def fetch_arb_routes_state(client):
    engine = await acquire_quote_cycle(client)

    async def pair(symbol_in, symbol_out, amount):
        tin, tout = TOKENS[symbol_in], TOKENS[symbol_out]
        if tin not in engine.decimals or tout not in engine.decimals:
            return None
        raw = int(Decimal(amount) * 10 ** engine.decimals[tin])
        buys = await engine.paths(tin, tout, raw)
        selected_buys = buys[:3]
        reverse_paths = await gather_quote_reads(
            *(engine.paths(tout, tin, int(buy["amount_out_raw"])) for buy in selected_buys)
        )
        routes = []
        for buy, sells in zip(selected_buys, reverse_paths):
            for sell in sells[:3]:
                if {leg["pool"] for leg in buy["legs"]} & {
                    leg["pool"] for leg in sell["legs"]
                }:
                    continue
                returned = int(sell["amount_out_raw"])
                routes.append(
                    {
                        "buy": buy,
                        "sell": sell,
                        "round_trip_output_raw": str(returned),
                        "gross_profit_raw": str(returned - raw),
                        "gross_spread_bps": float(
                            Decimal(returned - raw) * 10000 / raw
                        ),
                        "net_profit_usd": None,
                        "net_spread_bps": None,
                        "is_profitable": None,
                        "cost_status": "total_execution_gas_transfer_taxes_and_MEV_not_proven",
                    }
                )
        routes.sort(key=lambda route: int(route["round_trip_output_raw"]), reverse=True)
        return {
            "symbol": symbol_in + "/" + symbol_out,
            "amount_in_human": amount,
            "amount_in_raw": str(raw),
            "decimals_in": engine.decimals[tin],
            "routes": routes,
            "acquired_buy_quotes": buys,
            "status": "quoted" if routes else "no_independent_route_acquired",
            "spread_bps": routes[0]["gross_spread_bps"] if routes else None,
            "net_spread_bps": None,
            "timestamp_ms": engine.observed_at_ms,
            "ttl_seconds": 10,
            "evidence": {
                "chain": "eip155:8453",
                "block_number": int(engine.block, 16),
                "block_hash": engine.block_info["hash"],
                "method": "read_only_eth_call",
                "scope": "Distinct-pool paths at one block; no swap, global optimum or net profitability asserted",
                **quote_freshness(engine),
            },
        }

    results = await asyncio.gather(
        *(pair(*configuration) for configuration in ARB_PAIRS), return_exceptions=True
    )
    quote_freshness(engine)
    return [
        result
        for result in results
        if isinstance(result, dict) and result["acquired_buy_quotes"]
    ]
