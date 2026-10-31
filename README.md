# Pulse402 — Dual-Chain (Base + Solana) x402 Data Oracle

Pulse402 is an autonomous, high-frequency data oracle designed specifically for autonomous AI agents and trading bots. It streams verified, read-only market intelligence and pre-trade security checks over HTTP and MCP, settling micropayments natively over the **x402 V2 protocol** on both **Base L2 (`eip155:8453`)** and **Solana Mainnet (`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`)**.

The canonical service is hosted at `https://pulse402-oracle.onrender.com` and proxied globally with zero latency via Cloudflare Workers at `https://pulse402-oracle.ak10082247.workers.dev`.

Every response preserves its original observation timestamp and a strict 10-second acquisition TTL. Outages and stale quotes are reported transparently before payment settlement.

---

## 1. Live Feed Catalog & High-Margin Pricing

| Feed | Route | Tier | Price (USDC) | Atomic Units | Description |
| :--- | :--- | :---: | :---: | :---: | :--- |
| **Base DEX Liquidity** | `/v1/pulse/base-liquidity` | **Tier 1** | **$0.020** | `20,000` | Pinned-block Uniswap V3 & Aerodrome pool reserves, slippage quotes for $100 / $1k / $10k USDC, and pool balance shifts. |
| **GoPlus Threat Intel** | `/v1/pulse/threat-intel` | **Tier 1** | **$0.020** | `20,000` | Real-time honeypot flags, malicious spender checks, and deterministic prompt-injection text sanitization. |
| **Polymarket Velocity** | `/v1/pulse/polymarket-velocity` | **Tier 2** | **$0.050** | `50,000` | Live CLOB orderbook bid/ask depth, 5-minute probability movement, and momentum signals from real exchange price history. |
| **Cross-DEX Arb Routes** | `/v1/pulse/arb-routes` | **Tier 2** | **$0.050** | `50,000` | Distinct-pool gross round-trip quotes across Base pools with fee deductions and pinned-block execution state. |
| **Solana Pump.fun & Raydium** | `/v1/pulse/solana-velocity` | **Tier 2** | **$0.050** | `50,000` | Real-time Solana Pump.fun and Raydium token velocity, trending boosted tokens, 5m volume, and bonding curve status for sniper bots. |
| **HFT Session Pass** | `POST /v1/pass/prepay` | **Tier 3** | **$10.000** | `10,000,000` | **1,000 zero-latency calls** ($0.010/call effective rate — saves 50%–80% over per-call settlement and eliminates signing latency). |

---

## 2. Dual-Chain Settlement Infrastructure

Pulse402 accepts gasless, partial-signed x402 V2 authorizations on both major agent ecosystems:

### A. Base Mainnet (`eip155:8453`)
- **Asset**: Circle USDC (`0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`, 6 decimals)
- **Standard**: EIP-3009 `TransferWithAuthorization` (requires **zero gas / 0 ETH** from the buyer)
- **Merchant Recipient**: `0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6`
- **Settlement**: Verified through Coinbase CDP Facilitator & PayAI Facilitator (`https://facilitator.payai.network`)

### B. Solana Mainnet (`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`)
- **Asset**: Native SPL USDC (`EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v`, 6 decimals)
- **Standard**: SPL Token `TransferChecked` inside a sponsored v0 `VersionedTransaction` (requires **zero gas / 0 SOL** from the buyer)
- **Merchant Recipient**: `9wuMysgXwNhtQeqJgGByAiMYc4EjzZ5wUSmnjrrt3ffh`
- **Merchant USDC ATA**: `DD3A7jaXFeHcif9eYj6Vhomuxypat5Yxi8ow6wxz27yL`
- **Sponsored Fee Payer**: `CjNFTjvBhbJJd2B5ePPMHRLx1ELZpa8dwQgGL727eKww` (relayed via PayAI Facilitator)

---

## 3. High-Frequency Prepay Session Pass (`POST /v1/pass/prepay`)

High-frequency algorithmic bots and multi-agent swarms can eliminate on-chain signing latency and save 60%–80% on fees by activating a prepaid session pass:

1. **Purchase**: Send a one-time `$10.00 USDC` payment on Base or Solana to `POST /v1/pass/prepay`.
2. **Receipt**: Server returns a cryptographically secure `session_token` with 1,000 prepaid calls valid for 7 days.
3. **Execution**: Include the header `X-Pulse-Session: <token>` and `Idempotency-Key: <uuid>` on any paid endpoint.
4. **Zero Latency**: Calls execute instantly without waiting for on-chain signature verification or facilitator relay.

---

## 4. Model Context Protocol (MCP) & IDE Integration

Pulse402 runs a production Model Context Protocol (MCP) JSON-RPC 2.0 server at:
`https://pulse402-oracle.ak10082247.workers.dev/mcp`

### Connecting in Cursor, Windsurf, Zed, or Claude Desktop

Add this snippet to your IDE's MCP configuration:

```json
{
  "mcpServers": {
    "pulse402-oracle": {
      "url": "https://pulse402-oracle.ak10082247.workers.dev/mcp",
      "headers": {
        "MCP-Protocol-Version": "2026-07-28"
      }
    }
  }
}
```

### Available MCP Tools
- `pulse_solana_velocity`: Real-time Solana Pump.fun and Raydium token velocity, trending boosted tokens, and liquidity depth.
- `pulse_base_liquidity`: Mandatory pre-swap liquidity and slippage verification on Base DEXs (Aerodrome & Uniswap V3).
- `pulse_threat_intel`: Honeypot detection, blacklist checks, and prompt-injection text sanitization.
- `pulse_polymarket_velocity`: Active prediction market probability movement and CLOB orderbook depth.
- `pulse_arb_routes`: Pinned-block cyclical arbitrage route quotes and profit simulation.

---

## 5. Machine-Readable Discovery & Telemetry

- **x402 Manifest**: `https://pulse402-oracle.ak10082247.workers.dev/.well-known/x402.json`
- **MCP Manifest**: `https://pulse402-oracle.ak10082247.workers.dev/.well-known/mcp.json`
- **Agent Card**: `https://pulse402-oracle.ak10082247.workers.dev/.well-known/agent-card.json`
- **OpenAPI 3.1 Spec**: `https://pulse402-oracle.ak10082247.workers.dev/openapi.json`
- **LLM Context**: `https://pulse402-oracle.ak10082247.workers.dev/llms.txt`
- **Skill Doc**: `https://pulse402-oracle.ak10082247.workers.dev/skill.md`
- **Live Telemetry**: `https://pulse402-oracle.ak10082247.workers.dev/v1/telemetry/summary`

---

## 6. Autonomous Machine Distribution & Benchmarks

### A. MCP & x402 V2 Benchmark Suite (`benchmarks/`)
Run automated performance and payment compliance benchmarks for your agent framework:
```bash
python benchmarks/mcp_payment_benchmark.py --iterations 20
python benchmarks/ci_agent_test.py
```
- Benchmark dataset: `benchmarks/fixtures/mcp_eval_tasks.json` (compatible with SWE-bench, AgentBench, and GAIA harnesses).

### B. DEX Pre-Swap Slippage Verification Standard (`integrations/`)
- Guide: [`integrations/DEX_SLIPPAGE_FALLBACK_GUIDE.md`](file:///c:/Users/AK/Documents/money%20maker/pulse402-oracle/integrations/DEX_SLIPPAGE_FALLBACK_GUIDE.md) — Standardized Pre-Swap Verification Table for Aerodrome, Uniswap V3, Raydium, and Pump.fun.
- ElizaOS Plugin: [`integrations/elizaos/plugin-pulse402.ts`](file:///c:/Users/AK/Documents/money%20maker/pulse402-oracle/integrations/elizaos/plugin-pulse402.ts)
- Hummingbot Guard: [`integrations/trading_bot/pulse_preflight.py`](file:///c:/Users/AK/Documents/money%20maker/pulse402-oracle/integrations/trading_bot/pulse_preflight.py)
- LangChain / LangGraph Tools: [`integrations/langchain_pulse.py`](file:///c:/Users/AK/Documents/money%20maker/pulse402-oracle/integrations/langchain_pulse.py)

### C. Pre-Trade Smart Contract Revert Directive (`contracts/`)
- Live Base Mainnet Contract: [`0xC1d50CdE7a17162D20492B707e566B67b877970f`](https://basescan.org/address/0xC1d50CdE7a17162D20492B707e566B67b877970f) (Sourcify Exact Match Verified)
- Reference Contract: [`contracts/PulseGuardedLureToken.sol`](file:///c:/Users/AK/Documents/money%20maker/pulse402-oracle/contracts/PulseGuardedLureToken.sol)
- ABI / Searcher Metadata: [`contracts/BaseMevLureMetadata.json`](file:///c:/Users/AK/Documents/money%20maker/pulse402-oracle/contracts/BaseMevLureMetadata.json)
- MEV Revert Resolver: `python scripts/simulate_mev_lure.py`

