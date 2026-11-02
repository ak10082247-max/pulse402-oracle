#!/usr/bin/env python3
import asyncio
import json
from engine import poller

async def scan():
    client = poller._feed_client()
    try:
        print("=================================================================")
        print("  PULSE402 LIVE ALPHA & OPPORTUNITY SCANNER")
        print("=================================================================")
        
        # 1. Polymarket Velocity
        poly = await poller.fetch_polymarket_velocity_state(client)
        print("\n[1] Top Polymarket Velocity & Orderbook Signals:")
        items = poly if isinstance(poly, list) else (poly.get("data") if isinstance(poly, dict) else [])
        if items:
            for m in items[:6]:
                q = m.get("question") or m.get("slug") or "Market"
                vel = m.get("velocity_5m_pct") or 0.0
                vol = m.get("volume_24h_usd") or 0.0
                book = m.get("orderbook", {}) or {}
                bid_depth = book.get("bid_depth_usdc", "0") or "0"
                imbalance = book.get("imbalance_ratio", 0) or 0.0
                try:
                    bd_float = float(bid_depth)
                except Exception:
                    bd_float = 0.0
                print(f"  • {q[:45]:<46} | 5m Vel: {vel:+.1f}% | 24h Vol: ${vol:>9,.0f} | Imbalance: {imbalance:.2f} | Bid Depth: ${bd_float:,.0f}")
        else:
            print("  (Polymarket feed currently quiet or updating)")

        # 2. Solana Trending Velocity
        sol = await poller.fetch_solana_velocity_state(client)
        print("\n[2] Top Solana Pump.fun / Raydium Velocity Surges:")
        sol_items = sol if isinstance(sol, list) else (sol.get("data") if isinstance(sol, dict) else [])
        if sol_items:
            for t in sol_items[:6]:
                sym = t.get("symbol", "N/A")
                dex = t.get("dex", "N/A")
                chg = t.get("price_change_5m_pct")
                vol5m = t.get("volume_5m_usd") or 0.0
                liq = t.get("liquidity_usd") or 0.0
                chg_str = f"{chg:+.1f}%" if chg is not None else "N/A"
                print(f"  • {sym:<12} ({dex:<8}) | 5m Chg: {chg_str:>7} | 5m Vol: ${vol5m:>8,.0f} | Liq: ${liq:>8,.0f}")
        else:
            print("  (Solana feed currently quiet or updating)")

        # 3. Base Liquidity & Spreads
        liq = await poller.fetch_base_liquidity_state(client)
        print("\n[3] Base DEX Pool Reserves & Slippage:")
        liq_items = liq if isinstance(liq, list) else (liq.get("data") if isinstance(liq, dict) else [])
        if liq_items:
            for p in liq_items:
                sym = p.get("symbol")
                q_uni = p.get("quotes_usdc", {}).get("1k", "N/A")
                q_aero = p.get("quotes_usdc", {}).get("100", "N/A")
                print(f"  • {sym:<6} | 1k USDC Quote: {q_uni} | 100 USDC Quote: {q_aero}")
        else:
            print("  (Base DEX feed currently quiet or updating)")

    finally:
        await client.aclose()

if __name__ == "__main__":
    asyncio.run(scan())
