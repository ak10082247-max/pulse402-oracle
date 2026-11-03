# Pulse402 Oracle — Compliant Ecosystem Registry Submission Guide

This document contains standardized, verified schemas and pull request templates for registering the **Pulse402 Dual-Chain Oracle MCP Server** across institutional and open-source AI agent directories.

---

## 1. Official Model Context Protocol (MCP) Registry (`modelcontextprotocol/servers`)

### Submission Metadata (`pulse402-oracle.json`)
```json
{
  "name": "pulse402-oracle",
  "displayName": "Pulse402 Dual-Chain DeFi Oracle",
  "description": "Real-time on-chain market intelligence, Polymarket orderflow velocity, Base DEX slippage bounds, and GoPlus token risk checks via x402 V2.",
  "author": "AK | Kurox",
  "homepage": "https://pulse402-oracle.ak10082247.workers.dev",
  "repository": "https://github.com/ak10082247-max/pulse402-oracle",
  "license": "MIT",
  "transport": {
    "type": "http",
    "url": "https://pulse402-oracle.ak10082247.workers.dev/mcp"
  },
  "capabilities": {
    "tools": [
      {
        "name": "pulse_base_liquidity",
        "description": "Pre-swap liquidity reserves, pool balance shift, and pinned USDC quotes on Base (Uniswap V3 & Aerodrome)."
      },
      {
        "name": "pulse_polymarket_velocity",
        "description": "CLOB orderbook depth, 5-minute price delta, and volume surges across liquid prediction markets."
      },
      {
        "name": "pulse_threat_intel",
        "description": "Pre-trade malicious spender checks, honeypot verification, and prompt-injection firewall."
      },
      {
        "name": "pulse_solana_velocity",
        "description": "Real-time Solana Pump.fun and Raydium token velocity and trending volume surges."
      },
      {
        "name": "pulse_arb_routes",
        "description": "Cyclic round-trip quotes across distinct Base DEX pools with gross spread calculations."
      }
    ]
  },
  "authentication": {
    "type": "none_for_trial_or_session_header",
    "trialEndpoint": "https://pulse402-oracle.ak10082247.workers.dev/v1/trial/{feed}",
    "paidProtocol": "x402-v2",
    "supportedNetworks": [
      "eip155:8453",
      "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    ]
  }
}
```

---

## 2. Smithery (`smithery.yaml`) & Glama (`glama.json`) Verification

Both configuration files are already present at the root of `pulse402-oracle`:
- [`smithery.yaml`](https://github.com/ak10082247-max/pulse402-oracle/blob/main/smithery.yaml)
- [`glama.json`](https://github.com/ak10082247-max/pulse402-oracle/blob/main/glama.json)

### Smithery CLI Verification Command:
```bash
npx -y @smithery/cli test https://pulse402-oracle.ak10082247.workers.dev/mcp
```

### Glama Import:
Visit [glama.ai/mcp/servers](https://glama.ai/mcp/servers) and connect repository `https://github.com/ak10082247-max/pulse402-oracle`.

---

## 3. Pull Request Template for Open-Source Agent Boilerplates (`create-llama` / LangChain Templates)

### PR Title:
`feat(integrations): add Pulse402 Dual-Chain DeFi & Market Intelligence Oracle MCP tool`

### PR Description:
```markdown
## Summary
Adds Pulse402 Oracle as a pre-configured, low-latency market intelligence and pre-trade safety tool for autonomous agents.

## What is Pulse402?
Pulse402 provides real-time, uncacheable financial and security telemetry across Base L2 (`eip155:8453`) and Solana (`solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`):
- **Base DEX Pre-Swap Safety**: Validates pool liquidity and slippage bounds on Aerodrome and Uniswap V3 before executing trades.
- **Polymarket Velocity**: Real-time CLOB orderbook depth and 5m probability movement.
- **GoPlus Threat Intel**: Pre-trade honeypot simulation, blacklist checks, and injection firewall.
- **Solana Pump.fun Velocity**: Live trending token orderflow.

## Security & Supply Chain Compliance
- Fully compliant with OpenSSF / SLSA frameworks.
- CycloneDX 1.5 Software Bill of Materials (SBOM) published and verified: `sbom.cyclonedx.json`.
- Zero private key handling: read-only telemetry with built-in zero-auth 3-call free demo trial (`/v1/trial/*`).

## Configuration (JSON-RPC MCP)
```json
{
  "mcpServers": {
    "pulse402-oracle": {
      "type": "http",
      "url": "https://pulse402-oracle.ak10082247.workers.dev/mcp"
    }
  }
}
```

## Live Verification
- MCP Endpoint: `https://pulse402-oracle.ak10082247.workers.dev/mcp`
- OpenAPI Schema: `https://pulse402-oracle.ak10082247.workers.dev/openapi.json`
- Live Telemetry: `https://pulse402-oracle.ak10082247.workers.dev/v1/telemetry/summary`
```
