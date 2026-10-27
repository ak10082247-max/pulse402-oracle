# Pulse402 — The High-Frequency Consumable x402 Data Oracle

[![Keepalive & Freshness Validator](https://github.com/ak10082247-max/pulse402-oracle/actions/workflows/keepalive.yml/badge.svg)](https://github.com/ak10082247-max/pulse402-oracle/actions/workflows/keepalive.yml)
[![Cloudflare Workers](https://img.shields.io/badge/Edge-Cloudflare%20Workers-orange)](https://workers.cloudflare.com/)
[![Base Mainnet](https://img.shields.io/badge/Network-Base%20(8453)-blue)](https://base.org)
[![x402 Protocol](https://img.shields.io/badge/Protocol-x402%20v2-green)](https://x402.org)

> **Pulse402** is a high-frequency, strictly read-only market and security telemetry oracle built for autonomous AI agents, algorithmic trading bots, and Cloudflare's HTTP 402 ecosystem. Refreshes continuously every 15 seconds.

---

## 1. High-Frequency Consumable Feeds (10s TTL, `Cache-Control: no-store, max-age=10`)

All feeds return live, non-cacheable telemetry with cryptographic `timestamp_ms` and `ttl_seconds: 10`:

| Endpoint | Primary Domain & Sources | Price (USDC) | Description |
| :--- | :--- | :---: | :--- |
| **`/v1/pulse/base-liquidity`** | Aerodrome SlipStream & Uniswap v3 | `$0.001` | Live pool reserves, 60-second netflow velocity, buy/sell tax status, and slippage quotes for $100 / $1,000 / $10,000 trade sizes. |
| **`/v1/pulse/polymarket-velocity`** | Polymarket Gamma & CLOB Orderbook | `$0.001` | Prediction markets moving >2% in 5 minutes, bid/ask depth spread imbalances, and smart-money accumulation alerts. |
| **`/v1/pulse/threat-intel`** | BaseScan & DexScreener Heuristics | `$0.001` | Newly flagged Base honeypot contracts, malicious approval spenders, and deterministic prompt-injection sanitization. |
| **`/v1/pulse/arb-routes`** | Base Cross-DEX Smart Router | `$0.001` | Real-time price discrepancies, executable smart-router paths, and net profit in USDC after fees and Base L2 gas. |

---

## 2. Zero-Friction MCP Handshake (<10% Odel/Registry Risk Score)

* **Neutral Analytical Terminology**: All tool descriptions explicitly state that Pulse402 is a read-only market and security telemetry oracle. It holds zero custody, requires zero private keys, and executes zero state-altering transactions.
* **Strict Annotations**: Every tool carries `readOnlyHint: true`, `destructiveHint: false`, `idempotentHint: true`, and `openWorldHint: true`.
* **SSRF Guard**: Strict server-side request forgery protection blocking private IP ranges (`127.0.0.1`, `localhost`, `169.254.169.254`, `10.0.0.0/8`, `192.168.0.0/16`).

### Connecting to Claude Desktop / Cursor
```json
{
  "mcpServers": {
    "pulse402": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://pulse402-oracle.onrender.com/mcp"]
    }
  }
}
```

---

## 3. Pricing & High-Frequency Session Pass (`POST /v1/pass/prepay`)

* **Standard Pay-As-You-Go**: `$0.001 USDC` per call via standard x402 headers on Base Mainnet (`eip155:8453`).
* **Merchant Treasury**: `0x73279fa4BadA7CAC888c62CDa4f5c8104765f6f1`
* **Free Tier Quota**: 5 free preview calls per IP per day for automated directory indexing.
* **HFT Prepay Pass**: Trading agents can prepay `$5 USDC` at `POST /v1/pass/prepay` for a 6,000-call low-latency session token (`X-Pulse-Session`), enabling 0ms payment overhead on high-frequency 10-second polling loops.

---

## 4. Discovery Manifests
* x402 Pricing Manifest: `GET /.well-known/x402.json`
* Streamable MCP Manifest: `GET /.well-known/mcp.json`
* LLM Plaintext Documentation: `GET /llms.txt`
* OpenAPI 3.1 Spec: `GET /openapi.json`
