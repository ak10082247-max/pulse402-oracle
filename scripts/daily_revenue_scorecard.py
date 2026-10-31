#!/usr/bin/env python3
"""Daily Revenue & Fleet Telemetry Scorecard for Pulse402 Oracle.

Queries:
1. Edge & backend telemetry (/v1/telemetry/summary)
2. On-chain Base merchant USDC balances (0x80E0... & 0x7327...)
3. On-chain Solana merchant USDC ATA balances (DD3A... & 3shU...)
4. Apify Actor stats (wEgUU1GE6Esy8A9ov)
Generates an executive scorecard and sends via SMTP or prints summary.
"""
import argparse
from datetime import datetime, timezone
import email.utils
import json
import os
from pathlib import Path
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE_RPC_URLS = [
    "https://mainnet.base.org",
    "https://base.llamarpc.com",
    "https://base-rpc.publicnode.com",
]

SOLANA_RPC_URLS = [
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
]

BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
MERCHANT_BASE_PRIMARY = "0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6"
MERCHANT_BASE_LEGACY = "0x73279fa4BadA7CAC888c62CDa4f5c8104765f6f1"

MERCHANT_SOL_ATA_PRIMARY = "DD3A7jaXFeHcif9eYj6Vhomuxypat5Yxi8ow6wxz27yL"
MERCHANT_SOL_ATA_LEGACY = "3shUSbf9mkGabKqa2pdV7ZrstYvHozwusurLLuugf4dc"

TELEMETRY_URLS = [
    "https://pulse402-oracle.ak10082247.workers.dev/v1/telemetry/summary",
    "https://pulse402-oracle.onrender.com/v1/telemetry/summary",
]

APIFY_ACTOR_ID = "wEgUU1GE6Esy8A9ov"


def _get_ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()


def post_json_rpc(url: str, payload: dict, timeout: int = 10) -> dict | None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "Pulse402-Scorecard/1.0"})
    ctx = _get_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        try:
            unverified_ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=timeout, context=unverified_ctx) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass
    return None


def fetch_json(url: str, headers: dict | None = None, timeout: int = 10) -> dict | None:
    req_headers = {"User-Agent": "Pulse402-Scorecard/1.0"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers)
    ctx = _get_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
    except Exception:
        try:
            unverified_ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=timeout, context=unverified_ctx) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception:
            pass
    return None


def get_base_usdc_balance(wallet_address: str) -> float | None:
    # eth_call: balanceOf(address)
    # Selector: 0x70a08231
    clean_addr = wallet_address.lower().removeprefix("0x").zfill(64)
    calldata = f"0x70a08231{clean_addr}"
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": BASE_USDC, "data": calldata}, "latest"],
    }
    for rpc in BASE_RPC_URLS:
        res = post_json_rpc(rpc, payload)
        if res and "result" in res and isinstance(res["result"], str):
            try:
                raw_int = int(res["result"], 16)
                return raw_int / 1_000_000.0  # USDC has 6 decimals
            except Exception:
                pass
    return None


def get_solana_ata_balance(ata_address: str) -> float | None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTokenAccountBalance",
        "params": [ata_address],
    }
    for rpc in SOLANA_RPC_URLS:
        res = post_json_rpc(rpc, payload)
        if res and "result" in res and "value" in res["result"]:
            val = res["result"]["value"]
            ui_amount = val.get("uiAmount")
            if ui_amount is not None:
                return float(ui_amount)
    return None


def get_telemetry() -> dict:
    for url in TELEMETRY_URLS:
        data = fetch_json(url)
        if data and isinstance(data, dict) and data.get("status") == "ok":
            return data
    # Fallback to local SQLite read if remote endpoint unavailable
    try:
        from engine.telemetry import snapshot
        return {"status": "ok", "telemetry": snapshot()}
    except Exception:
        return {"status": "unavailable", "telemetry": {}}


def get_apify_stats(apify_token: str | None) -> dict:
    if not apify_token:
        return {"configured": False, "runs": "N/A", "status": "Token not provided"}
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}"
    headers = {"Authorization": f"Bearer {apify_token}"}
    data = fetch_json(url, headers=headers)
    if data and "data" in data:
        actor_data = data["data"]
        return {
            "configured": True,
            "title": actor_data.get("title"),
            "pricingModel": actor_data.get("pricingModel"),
            "stats": actor_data.get("stats", {}),
        }
    return {"configured": True, "status": "Fetched or pending"}


def generate_scorecard_text(base_primary: float | None, base_legacy: float | None,
                            sol_primary: float | None, sol_legacy: float | None,
                            telemetry: dict, apify: dict) -> str:
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    telem = telemetry.get("telemetry", {})
    challenges = telem.get("challenges_by_route", {})
    previews = telem.get("previews_by_route", {})
    mcp_calls = telem.get("mcp_tool_calls", {})
    settlements = telem.get("verified_settlements", {})
    passes = telem.get("active_session_passes", 0)
    top_agents = telem.get("top_user_agents", {})

    total_base = (base_primary or 0.0) + (base_legacy or 0.0)
    total_sol = (sol_primary or 0.0) + (sol_legacy or 0.0)
    grand_treasury = total_base + total_sol

    report = f"""===============================================================
PULSE402 DAILY REVENUE & TELEMETRY SCORECARD
Generated: {now_utc}
===============================================================

1. ON-CHAIN VERIFIED TREASURY (USDC)
---------------------------------------------------------------
Base L2 Primary ({MERCHANT_BASE_PRIMARY}):
  Balance: {f"{base_primary:,.6f} USDC" if base_primary is not None else "RPC Offline / Unreachable"}
Base L2 Legacy ({MERCHANT_BASE_LEGACY}):
  Balance: {f"{base_legacy:,.6f} USDC" if base_legacy is not None else "RPC Offline / Unreachable"}
Solana Primary ATA ({MERCHANT_SOL_ATA_PRIMARY}):
  Balance: {f"{sol_primary:,.6f} USDC" if sol_primary is not None else "RPC Offline / Unreachable"}
Solana Legacy ATA ({MERCHANT_SOL_ATA_LEGACY}):
  Balance: {f"{sol_legacy:,.6f} USDC" if sol_legacy is not None else "RPC Offline / Unreachable"}

Grand Treasury Total: ${grand_treasury:,.6f} USDC

2. PRICING TIERS & UNIT ECONOMICS
---------------------------------------------------------------
• Tier 1 (Pre-Trade Safety & Liquidity): $0.020 USDC (20,000 atomic units)
  - /v1/pulse/base-liquidity
  - /v1/pulse/threat-intel
• Tier 2 (Actionable Alpha & Arbitrage): $0.050 USDC (50,000 atomic units)
  - /v1/pulse/polymarket-velocity
  - /v1/pulse/arb-routes
• Tier 3 (HFT Session Pass): $10.00 USDC for 1,000 zero-latency calls
  - $0.010 effective rate per call (50%–80% savings for bots)
• Apify PPE Event Rate: $0.025 USD ($0.020 net after 20% commission)

3. FLEET TELEMETRY & TRAFFIC
---------------------------------------------------------------
Active HFT Session Passes: {passes}
Verified Settlements (Total):
  - Base (eip155:8453): {settlements.get("eip155:8453", 0)}
  - Solana (solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp): {settlements.get("solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp", 0)}

HTTP 402 Challenges by Route:
{chr(10).join(f"  - {k}: {v}" for k, v in challenges.items()) if challenges else "  - No 402 challenges recorded in cycle"}

Preview Hits by Route:
{chr(10).join(f"  - {k}: {v}" for k, v in previews.items()) if previews else "  - No preview hits recorded in cycle"}

MCP Tool Calls:
{chr(10).join(f"  - {k}: {v}" for k, v in mcp_calls.items()) if mcp_calls else "  - No MCP tool calls recorded in cycle"}

Top 10 Active User-Agents:
{chr(10).join(f"  - {k[:60]}: {v}" for k, v in list(top_agents.items())[:10]) if top_agents else "  - No user agent traffic recorded in cycle"}

4. APIFY STORE ACTOR STATUS (wEgUU1GE6Esy8A9ov)
---------------------------------------------------------------
Configuration: {json.dumps(apify, indent=2)}

===============================================================
End of Report — Pulse402 Autonomous Network
===============================================================
"""
    return report


def send_scorecard_email(smtp_user: str, smtp_pass: str, recipient: str, report_text: str):
    subject = f"Pulse402 Daily Revenue Scorecard — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
    msg = f"From: Pulse402 Telemetry <{smtp_user}>\r\n"
    msg += f"To: {recipient}\r\n"
    msg += f"Subject: {subject}\r\n"
    msg += f"Date: {email.utils.formatdate(localtime=True)}\r\n"
    msg += f"Message-ID: {email.utils.make_msgid(domain='pulse402-oracle.ak10082247.workers.dev')}\r\n"
    msg += "Content-Type: text/plain; charset=UTF-8\r\n\r\n"
    msg += report_text

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, [recipient], msg.encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="Pulse402 Daily Revenue Scorecard")
    parser.add_argument("--dry-run", action="store_true", help="Print scorecard without emailing")
    parser.add_argument("--email", type=str, help="Destination email for scorecard (defaults to SMTP_USER)")
    args = parser.parse_args()

    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASS")
    apify_token = os.getenv("APIFY_TOKEN")
    recipient = args.email or smtp_user

    print("Fetching Base USDC on-chain balances...")
    base_primary = get_base_usdc_balance(MERCHANT_BASE_PRIMARY)
    base_legacy = get_base_usdc_balance(MERCHANT_BASE_LEGACY)

    print("Fetching Solana USDC ATA on-chain balances...")
    sol_primary = get_solana_ata_balance(MERCHANT_SOL_ATA_PRIMARY)
    sol_legacy = get_solana_ata_balance(MERCHANT_SOL_ATA_LEGACY)

    print("Fetching fleet telemetry summary...")
    telemetry = get_telemetry()

    print("Fetching Apify stats...")
    apify = get_apify_stats(apify_token)

    report = generate_scorecard_text(base_primary, base_legacy, sol_primary, sol_legacy, telemetry, apify)

    print(report)

    if not args.dry_run and smtp_user and smtp_pass and recipient:
        print(f"Sending scorecard email to {recipient}...")
        try:
            send_scorecard_email(smtp_user, smtp_pass, recipient, report)
            print("Scorecard email sent successfully.")
        except Exception as e:
            sys.stderr.write(f"[ERROR] Failed to send email: {e}\n")
    else:
        print("[INFO] Dry run or SMTP credentials not configured; output printed to console.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
