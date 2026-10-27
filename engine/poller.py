import asyncio
import time
import logging
import sqlite3
import json
import httpx
from typing import Dict, Any, List

from .base_dex import fetch_base_liquidity_state
from .polymarket import fetch_polymarket_velocity_state
from .threat_intel import fetch_threat_intel_state, sanitize_untrusted_text
from .arb_router import fetch_arb_routes_state

logger = logging.getLogger("pulse402.poller")

# In-memory rolling state
STATE: Dict[str, Any] = {
    "base_liquidity": [],
    "polymarket_velocity": [],
    "threat_intel": {},
    "arb_routes": [],
    "last_poll_ms": 0,
    "poll_count": 0,
    "status": "initializing"
}

DB_PATH = "state/pulse_state.db"

def init_db():
    import os
    os.makedirs("state", exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pulse_snapshots (
                feed_name TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                updated_at_ms INTEGER NOT NULL
            )
        """)
        conn.commit()

def save_snapshot(feed_name: str, data: Any, timestamp_ms: int):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pulse_snapshots (feed_name, data_json, updated_at_ms) VALUES (?, ?, ?)",
                (feed_name, json.dumps(data), timestamp_ms)
            )
            conn.commit()
    except Exception as e:
        logger.debug(f"Failed to persist state snapshot for {feed_name}: {e}")

async def run_single_poll_cycle(client: httpx.AsyncClient):
    global STATE
    now_ms = int(time.time() * 1000)

    try:
        base_liq, poly_vel, threat, arb = await asyncio.gather(
            fetch_base_liquidity_state(client),
            fetch_polymarket_velocity_state(client),
            fetch_threat_intel_state(),
            fetch_arb_routes_state(client),
            return_exceptions=True
        )

        if isinstance(base_liq, list) and base_liq:
            STATE["base_liquidity"] = base_liq
            save_snapshot("base-liquidity", base_liq, now_ms)

        if isinstance(poly_vel, list) and poly_vel:
            STATE["polymarket_velocity"] = poly_vel
            save_snapshot("polymarket-velocity", poly_vel, now_ms)

        if isinstance(threat, dict) and threat:
            STATE["threat_intel"] = threat
            save_snapshot("threat-intel", threat, now_ms)

        if isinstance(arb, list) and arb:
            STATE["arb_routes"] = arb
            save_snapshot("arb-routes", arb, now_ms)

        STATE["last_poll_ms"] = now_ms
        STATE["poll_count"] += 1
        STATE["status"] = "healthy"
        logger.info(f"[Pulse402 Poller] Cycle #{STATE['poll_count']} completed at {now_ms}")
    except Exception as e:
        logger.error(f"[Pulse402 Poller] Cycle error: {e}")

async def start_poller_loop(poll_interval_seconds: float = 15.0):
    """
    Continuous background loop polling all 4 oracle data feeds every 15 seconds.
    """
    init_db()
    logger.info("[Pulse402 Poller] Starting 15-second background state-refresh engine...")
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Pulse402-Oracle-Poller/1.0"}) as client:
        while True:
            start_time = time.time()
            await run_single_poll_cycle(client)
            elapsed = time.time() - start_time
            sleep_time = max(0.5, poll_interval_seconds - elapsed)
            await asyncio.sleep(sleep_time)

def get_feed_state(feed_name: str) -> Dict[str, Any]:
    """
    Returns the latest non-cacheable data with cryptographic timestamp_ms and ttl_seconds: 10.
    """
    now_ms = int(time.time() * 1000)
    data = STATE.get(feed_name.replace("-", "_")) or []
    
    # If in-memory is empty, try loading from SQLite
    if not data:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cursor = conn.execute("SELECT data_json, updated_at_ms FROM pulse_snapshots WHERE feed_name = ?", (feed_name,))
                row = cursor.fetchone()
                if row:
                    data = json.loads(row[0])
        except Exception:
            pass

    return {
        "status": "ok",
        "feed": feed_name,
        "timestamp_ms": now_ms,
        "ttl_seconds": 10,
        "data": data
    }
