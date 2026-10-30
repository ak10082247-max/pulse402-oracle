# Pull Request: Add Pulse402 Data Oracle Toolkit to LangChain Community Tools

## Title
`feat(community): add Pulse402 Dual-Chain (Base + Solana) Data Oracle toolkit`

## Description
This pull request adds `Pulse402Toolkit` and individual tool wrappers to `langchain_community.tools.pulse402`, providing LLMs and autonomous agents with real-time, tamper-proof decentralized financial market telemetry and pre-trade security checks.

Pulse402 streams uncacheable 10s-TTL observations with micropayments settled via the x402 V2 protocol (supporting both Base L2 EIP-3009 and Solana SPL transfers).

### Included Tools
- `Pulse402SolanaVelocityTool`: Real-time Solana Pump.fun and Raydium token velocity and trending volume for sniper bots.
- `Pulse402BaseLiquidityTool`: Mandatory pre-swap liquidity and slippage verification on Base (Aerodrome & Uniswap V3).
- `Pulse402ThreatIntelTool`: Honeypot detection, blacklist verification, and prompt-injection firewall.
- `Pulse402PolymarketVelocityTool`: Prediction market probability movement and CLOB orderbook depth.
- `Pulse402ArbRoutesTool`: Pinned-block cross-DEX cyclic arbitrage routes.

### Python Usage Example
```python
from langchain_community.agent_toolkits import Pulse402Toolkit

toolkit = Pulse402Toolkit(
    endpoint="https://pulse402-oracle.ak10082247.workers.dev",
    session_token="optional_hft_prepay_session_token",
)
tools = toolkit.get_tools()

# Pass directly to LangChain agents or OpenAI Function Calling
agent = initialize_agent(
    tools,
    llm,
    agent=AgentType.STRUCTURED_CHAT_ZERO_SHOT_REACT_DESCRIPTION,
    verbose=True
)
```

### TypeScript / LangChain.js Usage Example
```typescript
import { Pulse402Toolkit } from "@langchain/community/tools/pulse402";

const toolkit = new Pulse402Toolkit({
  endpoint: "https://pulse402-oracle.ak10082247.workers.dev",
});
const tools = toolkit.getTools();
```

### Live Validation & Edge Endpoint
- Canonical Edge URL: `https://pulse402-oracle.ak10082247.workers.dev`
- MCP 2.0 Endpoint: `https://pulse402-oracle.ak10082247.workers.dev/mcp`
- Free Developer Previews: `https://pulse402-oracle.ak10082247.workers.dev/v1/preview/base-liquidity`
