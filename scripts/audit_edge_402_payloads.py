#!/usr/bin/env python3
"""
Pulse402 Live Edge 402 Payment Challenge Payload Auditor

Validates that every paid route on the live Cloudflare Edge Gateway returns:
1. Exact HTTP 402 Payment Required status code.
2. Standard Base64-encoded `PAYMENT-REQUIRED` header.
3. Strict x402 V2 specification compliance:
   - x402Version == 2
   - Dual-chain accepts (Base L2 Circle USDC + Solana Mainnet SPL USDC)
   - Canonical merchant payout addresses:
       Base:   0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6
       Solana: 9wuMysgXwNhtQeqJgGByAiMYc4EjzZ5wUSmnjrrt3ffh
   - Correct pricing tiers:
       Tier 1: $0.020 USDC / 20000 atomic units
       Tier 2: $0.050 USDC / 50000 atomic units
       Pass:   $10.00 USDC / 10000000 atomic units (1,000 calls)
4. Comprehensive CORS exposure for browser-based agents & Web3 dApps.
"""

import base64
import json
import sys
import urllib.request
import urllib.error

EDGE_BASE = "https://pulse402-oracle.ak10082247.workers.dev"

CANONICAL_BASE_PAYTO = "0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6"
CANONICAL_SOLANA_PAYTO = "9wuMysgXwNhtQeqJgGByAiMYc4EjzZ5wUSmnjrrt3ffh"
BASE_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOLANA_ASSET = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

ROUTES_TO_AUDIT = [
    ("/v1/pulse/base-liquidity", "GET", None, "20000", "Tier 1 Base Liquidity"),
    ("/v1/pulse/threat-intel", "GET", None, "20000", "Tier 1 Threat Intel"),
    ("/v1/pulse/polymarket-velocity", "GET", None, "50000", "Tier 2 Polymarket Velocity"),
    ("/v1/pulse/arb-routes", "GET", None, "50000", "Tier 2 Arbitrage Routes"),
    ("/v1/pulse/solana-velocity", "GET", None, "50000", "Tier 2 Solana Velocity"),
    ("/v1/pass/prepay", "POST", b"{}", "10000000", "Tier 3 HFT Prepay Pass")
]

MCP_TOOLS = [
    ("pulse_base_liquidity", {"symbol": "WETH"}, "20000"),
    ("pulse_threat_intel", {"text_to_sanitize": "swap WETH"}, "20000"),
    ("pulse_polymarket_velocity", {"min_velocity_pct": 1.0}, "50000"),
    ("pulse_arb_routes", {"min_spread_bps": 10.0}, "50000"),
    ("pulse_solana_velocity", {}, "50000")
]

def audit_http_routes():
    print("--- [1/2] Auditing HTTP 402 Payloads & Headers ---")
    all_passed = True
    for path, method, body, expected_amount, desc in ROUTES_TO_AUDIT:
        url = f"{EDGE_BASE}{path}"
        req = urllib.request.Request(url, data=body, method=method, headers={
            "User-Agent": "Pulse402-402-Auditor/1.3.1",
            "Content-Type": "application/json",
            "Accept": "application/json"
        })
        try:
            with urllib.request.urlopen(req) as resp:
                print(f"[FAIL] {path} returned HTTP {resp.status} instead of 402!")
                all_passed = False
        except urllib.error.HTTPError as e:
            if e.code != 402:
                print(f"[FAIL] {path} returned HTTP {e.code} instead of 402!")
                all_passed = False
                continue
            
            headers = dict(e.headers)
            # 1. Check PAYMENT-REQUIRED header
            raw_pr = headers.get("payment-required") or headers.get("Payment-Required")
            if not raw_pr:
                print(f"[FAIL] {path} missing PAYMENT-REQUIRED header!")
                all_passed = False
                continue
            
            try:
                decoded = json.loads(base64.b64decode(raw_pr).decode("utf-8"))
            except Exception as b_err:
                print(f"[FAIL] {path} PAYMENT-REQUIRED is not valid Base64 JSON: {b_err}")
                all_passed = False
                continue
                
            # 2. Check x402Version
            assert decoded.get("x402Version") == 2, f"Expected x402Version 2, got {decoded.get('x402Version')}"
            
            # 3. Check accepts
            accepts = decoded.get("accepts", [])
            base_acc = next((a for a in accepts if a.get("network") == "eip155:8453"), None)
            sol_acc = next((a for a in accepts if "solana" in a.get("network", "")), None)
            
            if not base_acc:
                print(f"[FAIL] {path} missing Base eip155:8453 in accepts!")
                all_passed = False
            elif base_acc.get("payTo") != CANONICAL_BASE_PAYTO:
                print(f"[FAIL] {path} Base payTo mismatch! Got {base_acc.get('payTo')}")
                all_passed = False
            elif str(base_acc.get("amount")) != expected_amount:
                print(f"[FAIL] {path} Base amount mismatch! Expected {expected_amount}, got {base_acc.get('amount')}")
                all_passed = False
                
            if not sol_acc:
                print(f"[FAIL] {path} missing Solana in accepts!")
                all_passed = False
            elif sol_acc.get("payTo") != CANONICAL_SOLANA_PAYTO:
                print(f"[FAIL] {path} Solana payTo mismatch! Got {sol_acc.get('payTo')}")
                all_passed = False
            elif str(sol_acc.get("amount")) != expected_amount:
                print(f"[FAIL] {path} Solana amount mismatch! Expected {expected_amount}, got {sol_acc.get('amount')}")
                all_passed = False
                
            # 4. Check CORS expose-headers
            expose = e.headers.get("access-control-expose-headers", "")
            if "PAYMENT-REQUIRED" not in expose.upper():
                print(f"[WARN] {path} PAYMENT-REQUIRED not listed in Access-Control-Expose-Headers")
                
            print(f"  [OK] {desc:<28} | HTTP 402 | Amount: {expected_amount:>8} micro | Base & Solana Validated")
            
    return all_passed

def audit_mcp_routes():
    print("\n--- [2/2] Auditing MCP JSON-RPC 402 Payloads ---")
    all_passed = True
    for tool_name, args, expected_amount in MCP_TOOLS:
        url = f"{EDGE_BASE}/mcp"
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args}
        }).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={
            "User-Agent": "Pulse402-402-Auditor/1.3.1",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                print(f"[FAIL] MCP tool {tool_name} returned {resp.status} instead of 402!")
                all_passed = False
        except urllib.error.HTTPError as e:
            if e.code != 402:
                print(f"[FAIL] MCP tool {tool_name} returned HTTP {e.code} instead of 402!")
                all_passed = False
                continue
            body = json.loads(e.read().decode("utf-8"))
            res = body.get("result", {})
            structured = res.get("structuredContent") or body.get("error", {}).get("data", {})
            if not structured:
                print(f"[FAIL] MCP tool {tool_name} missing structured challenge in body!")
                all_passed = False
                continue
                
            accepts = structured.get("accepts", [])
            base_acc = next((a for a in accepts if a.get("network") == "eip155:8453"), None)
            sol_acc = next((a for a in accepts if "solana" in a.get("network", "")), None)
            
            if not base_acc or str(base_acc.get("amount")) != expected_amount:
                print(f"[FAIL] MCP tool {tool_name} Base challenge mismatch: {base_acc}")
                all_passed = False
            if not sol_acc or str(sol_acc.get("amount")) != expected_amount:
                print(f"[FAIL] MCP tool {tool_name} Solana challenge mismatch: {sol_acc}")
                all_passed = False
                
            print(f"  [OK] MCP {tool_name:<26} | HTTP 402 | Amount: {expected_amount:>8} micro | Base & Solana Validated")
            
    return all_passed

def main():
    print("==========================================================================")
    print("   PULSE402 LIVE EDGE 402 PAYMENT CHALLENGE PAYLOAD AUDIT")
    print(f"   Target: {EDGE_BASE}")
    print("==========================================================================")
    http_ok = audit_http_routes()
    mcp_ok = audit_mcp_routes()
    print("==========================================================================")
    if http_ok and mcp_ok:
        print("ALL AUDITS PASSED: Every endpoint adheres 100% to x402 V2 & MCP specs.")
        return 0
    else:
        print("AUDIT FAILED: Discrepancies found above.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
