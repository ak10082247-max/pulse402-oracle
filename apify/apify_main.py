import asyncio
import httpx
from apify import Actor

ORACLE_BASE_URL = "https://pulse402-oracle.onrender.com"

async def main():
    async with Actor:
        actor_input = await Actor.get_input() or {}
        feed_choice = actor_input.get("feed", "all")

        endpoints = []
        if feed_choice in ("all", "base_liquidity"):
            endpoints.append("/v1/pulse/base-liquidity")
        if feed_choice in ("all", "polymarket_velocity"):
            endpoints.append("/v1/pulse/polymarket-velocity")
        if feed_choice in ("all", "threat_intel"):
            endpoints.append("/v1/pulse/threat-intel")
        if feed_choice in ("all", "arb_routes"):
            endpoints.append("/v1/pulse/arb-routes")

        async with httpx.AsyncClient(timeout=10.0) as client:
            for ep in endpoints:
                try:
                    res = await client.get(f"{ORACLE_BASE_URL}{ep}")
                    if res.status_code == 200:
                        await Actor.push_data(res.json())
                except Exception as e:
                    Actor.log.warning(f"Failed to fetch {ep}: {e}")

if __name__ == "__main__":
    asyncio.run(main())
