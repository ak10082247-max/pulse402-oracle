"""Real-time paywall and gateway telemetry counters for Pulse402."""
import os
import sqlite3
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict

_LOCK = threading.Lock()
_CHALLENGES: Counter = Counter()
_PREVIEWS: int = 0
_MCP_TOOL_CALLS: int = 0
_SETTLEMENTS: Counter = Counter()
_USER_AGENTS: Counter = Counter()


def record_challenge(route: str) -> None:
    norm = route.removeprefix("/v1/pulse/").replace("_", "-").split("?")[0].strip()
    with _LOCK:
        _CHALLENGES[norm] += 1


def record_preview() -> None:
    global _PREVIEWS
    with _LOCK:
        _PREVIEWS += 1


def record_mcp_tool_call() -> None:
    global _MCP_TOOL_CALLS
    with _LOCK:
        _MCP_TOOL_CALLS += 1


def record_settlement(network: str) -> None:
    net = "base" if "8453" in str(network) else "solana" if "solana" in str(network) else str(network)
    with _LOCK:
        _SETTLEMENTS[net] += 1


def record_user_agent(ua: str) -> None:
    if not ua or not isinstance(ua, str):
        return
    clean_ua = ua[:128].strip()
    if clean_ua:
        with _LOCK:
            _USER_AGENTS[clean_ua] += 1


def get_active_sessions_count() -> int:
    try:
        db_path = Path(os.getenv("PULSE_PAYMENTS_DB", "state/payments.sqlite3"))
        if not db_path.exists():
            return 0
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM sessions WHERE expires > ? AND remaining > 0", (time.time(),))
            row = cur.fetchone()
            return int(row[0]) if row else 0
    except Exception:
        return 0


def get_db_settlement_counts() -> Dict[str, int]:
    try:
        db_path = Path(os.getenv("PULSE_PAYMENTS_DB", "state/payments.sqlite3"))
        if not db_path.exists():
            return {"base": 0, "solana": 0}
        with sqlite3.connect(str(db_path), timeout=5) as conn:
            cur = conn.cursor()
            cur.execute("SELECT receipt FROM purchases WHERE state IN ('paid', 'paid_pending_fulfillment')")
            base_count = 0
            sol_count = 0
            for (receipt_json,) in cur.fetchall():
                if receipt_json:
                    if "8453" in receipt_json:
                        base_count += 1
                    elif "solana" in receipt_json:
                        sol_count += 1
            return {"base": base_count, "solana": sol_count}
    except Exception:
        return {"base": 0, "solana": 0}


def get_summary() -> Dict[str, Any]:
    with _LOCK:
        db_settlements = get_db_settlement_counts()
        base_settled = max(_SETTLEMENTS["base"], db_settlements.get("base", 0))
        sol_settled = max(_SETTLEMENTS["solana"], db_settlements.get("solana", 0))

        top_uas = [
            {"user_agent": ua, "count": count}
            for ua, count in _USER_AGENTS.most_common(10)
        ]

        return {
            "service": "pulse402-oracle",
            "version": "1.3.0",
            "timestamp_ms": int(time.time() * 1000),
            "challenges_served": {
                "base-liquidity": _CHALLENGES.get("base-liquidity", 0),
                "polymarket-velocity": _CHALLENGES.get("polymarket-velocity", 0),
                "threat-intel": _CHALLENGES.get("threat-intel", 0),
                "arb-routes": _CHALLENGES.get("arb-routes", 0),
                "total": sum(_CHALLENGES.values()),
            },
            "free_preview_calls": _PREVIEWS,
            "mcp_tool_calls": _MCP_TOOL_CALLS,
            "settlements": {
                "base": base_settled,
                "solana": sol_settled,
                "total": base_settled + sol_settled,
            },
            "active_hft_session_passes": get_active_sessions_count(),
            "top_user_agents": top_uas,
        }
