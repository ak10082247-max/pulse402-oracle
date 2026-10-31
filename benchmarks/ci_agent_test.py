#!/usr/bin/env python3
"""
CI/CD Automated Agent Evaluation Test Runner

Evaluates AI agent tools and x402 payment negotiation against Pulse402 Oracle.
Can be invoked in GitHub Actions, GitLab CI, or local test harnesses to verify
that agent tool-calling pipelines correctly handle MCP JSON-RPC 2.0 and
dual-chain x402 V2 payment challenges.

Exit code 0 indicates 100% compliance.
"""

import json
import sys
import urllib.request
import urllib.error

ENDPOINT = "https://pulse402-oracle.ak10082247.workers.dev/mcp"

def call_mcp(method: str, params: dict, req_id: int = 1) -> tuple[int, dict]:
    payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode("utf-8")
    req = urllib.request.Request(ENDPOINT, data=payload, headers={
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Pulse402-CI-Agent-Eval/1.3.1"
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:
            return e.code, {"error": str(e)}

def test_protocol_compliance():
    print("[CI TEST 1/4] Checking MCP JSON-RPC initialize...")
    status, body = call_mcp("initialize", {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "ci-test-runner", "version": "1.0.0"}
    })
    assert status == 200, f"Expected 200, got {status}"
    assert "result" in body and body["result"].get("protocolVersion") == "2024-11-05"
    print("  -> PASS: initialize handshake valid")

def test_tools_listing():
    print("[CI TEST 2/4] Checking MCP tools/list...")
    status, body = call_mcp("tools/list", {})
    assert status == 200, f"Expected 200, got {status}"
    tools = body.get("result", {}).get("tools", [])
    names = {t["name"] for t in tools}
    expected = {"pulse_base_liquidity", "pulse_threat_intel", "pulse_polymarket_velocity", "pulse_arb_routes", "pulse_solana_velocity"}
    assert expected.issubset(names), f"Missing tools: {expected - names}"
    print(f"  -> PASS: all 5 core tools available ({', '.join(sorted(names))})")

def test_x402_dual_chain_negotiation():
    print("[CI TEST 3/4] Checking dual-chain x402 challenge response for paid tool calls...")
    status, body = call_mcp("tools/call", {
        "name": "pulse_base_liquidity",
        "arguments": {"symbol": "WETH"}
    })
    # HTTP status must be 402 Payment Required
    assert status == 402, f"Expected HTTP status 402, got {status}"
    
    # Challenge details can be in result.structuredContent or error.data
    res = body.get("result", {})
    structured = res.get("structuredContent") or body.get("error", {}).get("data", {})
    assert structured.get("error") == "payment_required" or structured.get("status") == 402, "Challenge must signal payment_required"
    
    accepts = structured.get("accepts", [])
    networks = {a.get("network") for a in accepts}
    assert "eip155:8453" in networks, "Missing Base network (eip155:8453)"
    assert "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp" in networks, "Missing Solana network"
    print(f"  -> PASS: dual-chain 402 challenge verified (Base + Solana)")

def test_preview_fallback():
    print("[CI TEST 4/4] Checking free preview endpoint fallback availability...")
    req = urllib.request.Request("https://pulse402-oracle.ak10082247.workers.dev/v1/preview/base-liquidity", headers={
        "User-Agent": "Pulse402-CI-Agent-Eval/1.3.1"
    })
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert "symbol" in data or "coverage" in data or "warmup_complete" in data or "status" in data
    print("  -> PASS: free preview fallback is live and healthy")

def main():
    print("=================================================================")
    print("   PULSE402 CI/CD MCP PROTOCOL & PAYMENT VERIFICATION SUITE")
    print("=================================================================")
    try:
        test_protocol_compliance()
        test_tools_listing()
        test_x402_dual_chain_negotiation()
        test_preview_fallback()
        print("\nALL CI CHECKS PASSED: Agent runtime is fully payment-ready.\n")
        return 0
    except AssertionError as e:
        print(f"\nFAIL: {e}\n", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"\nERROR: {e}\n", file=sys.stderr)
        return 2

if __name__ == "__main__":
    sys.exit(main())
