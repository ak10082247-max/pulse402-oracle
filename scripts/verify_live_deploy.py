import base64
import json
import ssl
import sys
import urllib.error
import urllib.request

ctx = ssl._create_unverified_context()
BASE_URL = "https://pulse402-oracle.ak10082247.workers.dev"

def test_endpoint(path, expected_amount, method="GET"):
    data = b"{}" if method == "POST" else None
    headers = {"User-Agent": "Verification-Suite/1.0"}
    if method == "POST":
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        urllib.request.urlopen(req, timeout=10, context=ctx)
        print(f"[FAIL] {path}: expected 402, got 200")
        return False
    except urllib.error.HTTPError as e:
        if e.code != 402:
            print(f"[FAIL] {path}: expected 402, got {e.code}")
            return False
        payment_required = e.headers.get("payment-required")
        if not payment_required:
            print(f"[FAIL] {path}: missing payment-required header")
            return False
        parsed = json.loads(base64.b64decode(payment_required))
        accepts = parsed.get("accepts", [])
        base_quote = next((a for a in accepts if a["network"] == "eip155:8453"), None)
        sol_quote = next((a for a in accepts if a["network"] == "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"), None)
        
        if not base_quote or base_quote["amount"] != str(expected_amount):
            print(f"[FAIL] {path}: Base amount expected {expected_amount}, got {base_quote.get('amount') if base_quote else None}")
            return False
        if not sol_quote or sol_quote["amount"] != str(expected_amount):
            print(f"[FAIL] {path}: Solana amount expected {expected_amount}, got {sol_quote.get('amount') if sol_quote else None}")
            return False
        print(f"[PASS] {path}: HTTP 402 received with required amount {expected_amount} on both Base & Solana")
        return True

print("=== LIVE EDGE PRICING VERIFICATION ===")
p1 = test_endpoint("/v1/pulse/base-liquidity", 20000)
p2 = test_endpoint("/v1/pulse/threat-intel", 20000)
p3 = test_endpoint("/v1/pulse/polymarket-velocity", 50000)
p4 = test_endpoint("/v1/pulse/arb-routes", 50000)
p5 = test_endpoint("/v1/pass/prepay", 10000000, method="POST")

print("\n=== LIVE TELEMETRY VERIFICATION ===")
req_tel = urllib.request.Request(f"{BASE_URL}/v1/telemetry/summary", headers={"User-Agent": "Verification-Suite/1.0"})
with urllib.request.urlopen(req_tel, timeout=10, context=ctx) as r:
    print(f"Status: {r.status}")
    print(r.read().decode())

print("\n=== LIVE GLAMA MANIFEST VERIFICATION ===")
req_glama = urllib.request.Request(f"{BASE_URL}/.well-known/glama.json", headers={"User-Agent": "Verification-Suite/1.0"})
with urllib.request.urlopen(req_glama, timeout=10, context=ctx) as r:
    print(f"Status: {r.status}")
    print(r.read().decode())
