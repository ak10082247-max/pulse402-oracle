#!/usr/bin/env python3
"""Pings and syndicates Pulse402 Oracle into MCP & x402 directories:

1. Smithery.ai (MCP registry)
2. Glama.ai (MCP registry)
3. mcp.so (Community MCP registry)
4. x402scan.com & PayAI Bazaar discovery indexers
"""
import argparse
import json
import ssl
import sys
import urllib.error
import urllib.request

REGISTRIES = [
    {
        "name": "PayAI Discovery Indexer",
        "url": "https://facilitator.payai.network/discovery/resources",
        "method": "GET",
    },
    {
        "name": "Smithery.ai Webhook / Registry Ping",
        "url": "https://smithery.ai/api/mcp/ping",
        "method": "POST",
        "payload": {
            "repository": "ak10082247-max/pulse402-oracle",
            "serverUrl": "https://pulse402-oracle.ak10082247.workers.dev/mcp",
        },
    },
    {
        "name": "Glama.ai Server Indexer",
        "url": "https://glama.ai/api/mcp/index",
        "method": "POST",
        "payload": {
            "repository": "ak10082247-max/pulse402-oracle",
            "glamaJsonUrl": "https://pulse402-oracle.ak10082247.workers.dev/.well-known/glama.json",
        },
    },
    {
        "name": "x402scan Discovery Ping",
        "url": "https://x402scan.com/api/servers/ping",
        "method": "POST",
        "payload": {
            "x402Url": "https://pulse402-oracle.ak10082247.workers.dev/.well-known/x402.json",
        },
    },
]


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


def ping_registry(entry: dict) -> dict:
    name = entry["name"]
    url = entry["url"]
    method = entry.get("method", "GET")
    payload = entry.get("payload")

    headers = {
        "User-Agent": "Pulse402-Registry-Syndication/1.3.0",
        "Content-Type": "application/json",
    }

    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = _get_ssl_context()

    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return {"name": name, "status": resp.status, "message": "Success"}
    except urllib.error.HTTPError as e:
        # A 200, 201, 202, or even 404/405/400 (if endpoint changed schema) proves network reachability
        return {"name": name, "status": e.code, "message": f"HTTP {e.code}"}
    except Exception as e:
        # Fallback to unverified context
        try:
            unverified_ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(req, timeout=10, context=unverified_ctx) as resp:
                return {"name": name, "status": resp.status, "message": "Success (unverified SSL)"}
        except Exception as e2:
            return {"name": name, "status": "ERROR", "message": str(e2)}


def main():
    parser = argparse.ArgumentParser(description="Pulse402 Registry Syndication")
    parser.add_argument("--dry-run", action="store_true", help="Log pings without making outbound network requests")
    args = parser.parse_args()

    print("=== Pulse402 MCP & x402 Registry Syndication ===")
    results = []

    for reg in REGISTRIES:
        if args.dry_run:
            print(f"[DRY RUN] Would ping {reg['name']} ({reg['url']})")
            results.append({"name": reg["name"], "status": "DRY_RUN", "message": "Simulated"})
        else:
            print(f"Pinging {reg['name']}...")
            res = ping_registry(reg)
            print(f"  -> Result: {res['status']} ({res['message']})")
            results.append(res)

    print("\nSyndication summary:")
    print(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
