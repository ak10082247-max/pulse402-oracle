# Pull Request: Add Pulse402 Dual-Chain Oracle Plugin (`@elizaos/plugin-pulse402`)

## Title
`feat(plugins): add Pulse402 Dual-Chain Oracle plugin for real-time Solana Pump.fun, Polymarket, and Base DEX telemetry`

## Description
This PR contributes `@elizaos/plugin-pulse402`, an autonomous market and threat intelligence plugin for Eliza agents operating on **Base L2 (`eip155:8453`)** and **Solana Mainnet (`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`)**.

Pulse402 enables Eliza trading agents, DeFi bots, and risk assessors to pull verified, unmanipulated on-chain observations and orderflow velocity using the gasless x402 V2 protocol (supporting Circle USDC on Base and SPL TokenChecked on Solana).

### Key Features
1. **Solana Velocity Alpha (`pulse_solana_velocity`)**:
   - Real-time Pump.fun bonding curve progression and Raydium token velocity.
   - 5-minute volume surges and price delta for meme coin sniper and copy-trading agents.
2. **Base DEX Pre-Swap Safety (`pulse_base_liquidity`)**:
   - Pinned-block Uniswap V3 and Aerodrome pool balance shift detection.
   - Verifies exact $100, $1k, and $10k USDC slippage quotes before executing swaps to avoid sandwich attacks.
3. **GoPlus Threat Intel & Injection Firewall (`pulse_threat_intel`)**:
   - Pre-trade honeypot simulation and malicious spender checks.
   - Built-in prompt injection and wallet-drain text sanitizer.
4. **Polymarket Velocity (`pulse_polymarket_velocity`)**:
   - CLOB orderbook bid/ask depth and 5-minute price delta from real exchange history.
5. **Cross-DEX Arbitrage Routes (`pulse_arb_routes`)**:
   - Cyclic round-trip quotes across distinct Base pools with gross spread calculations.

### Installation & Usage in ElizaOS
```typescript
import { pulse402Plugin } from "@elizaos/plugin-pulse402";

// Add to your agent configuration
const agent = new Agent({
  plugins: [
    pulse402Plugin({
      endpoint: "https://pulse402-oracle.ak10082247.workers.dev",
      // Optional: High-Frequency Prepay Session Token for zero-latency execution
      sessionToken: process.env.PULSE402_SESSION_TOKEN
    })
  ]
});
```

### Verification & Documentation
- Live MCP Server: `https://pulse402-oracle.ak10082247.workers.dev/mcp`
- OpenAPI Specification: `https://pulse402-oracle.ak10082247.workers.dev/openapi.json`
- x402 Protocol Specification: `https://pulse402-oracle.ak10082247.workers.dev/.well-known/x402.json`
- Zero-cost Free Previews: `GET /v1/preview/*`
