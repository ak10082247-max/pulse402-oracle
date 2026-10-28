# Pulse402 Oracle

Read-only Base market quotes, Polymarket books/history and sampled token-risk evidence for agents. The canonical service is https://pulse402-oracle.onrender.com. Feeds retain their original observation timestamps and a 10-second acquisition TTL; expired data is refreshed before delivery. Provider failures are reported before settlement.

| Feed | Acquired output | USDC/call |
| --- | --- | --- |
| `/v1/pulse/base-liquidity` | Observed liquidity, actual pool balances, measured balance changes after warm-up and pinned-block $100/$1,000/$10,000 USDC quotes on supported pools | 0.001 |
| `/v1/pulse/polymarket-velocity` | Actual CLOB depth/midpoint and five-minute probability movement from price history, with separate exchange and acquisition times | 0.001 |
| `/v1/pulse/threat-intel` | GoPlus assessments for the disclosed token sample, evidence hashes and scoped text checks | 0.001 |
| `/v1/pulse/arb-routes` | Distinct-pool, amount-specific round-trip quotes at one verified Base block, with gross spread | 0.001 |

Quotes are unsigned public contract reads. Total execution gas, transfer taxes, MEV, successful fills and net profitability are not established. Unsupported pools and missing evidence are explicit. Displayed price movement does not identify whales. The text sanitizer detects stated patterns and cannot guarantee complete injection prevention. An empty spender list is not an all-clear.

## Payments

Base and Solana use x402 v2 exact USDC payments. An unpaid request returns HTTP 402 with current server requirements. Use an x402-capable client; this server never needs a customer's private key.

- Base receiver: `0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6`
- Solana receiver: `9wuMysgXwNhtQeqJgGByAiMYc4EjzZ5wUSmnjrrt3ffh`
- Solana USDC ATA: `DD3A7jaXFeHcif9eYj6Vhomuxypat5Yxi8ow6wxz27yL`. It must exist before sponsored USDC transfers; a quote alone does not prove account readiness.
- `POST /v1/pass/prepay` with an empty JSON object purchases 6,000 credits for 5 USDC, valid for seven days. Credits activate only after confirmed settlement.
- Send a purchased token in `X-Pulse-Session`, with a unique `Idempotency-Key` for each delivery. Retrying identical arguments with the same key recovers the response without another credit.
- Use the original signed authorization and identical request to recover a pay-per-request result. Never sign a replacement payment when the response says `charged: "unknown"`.

The HTTP, sanitizer, prepay and MCP routes share one verified lifecycle: authenticate buyer, claim operation, verify, acquire a valid bounded result, persist preparation, submit settlement once, then commit the receipt/delivery. Ambiguous settlements are retained for reconciliation and never automatically submitted again. Retained completed purchases can recover after authorization expiry.

Set `PULSE_PAYMENTS_DB=/app/state/payments.sqlite3` on a persistent disk. SQLite uses WAL, full synchronization and owner-fenced operation transitions. The approved Render disk mounts `/app/state`. Ephemeral storage is unsuitable for preserving purchased credits or recovery across redeployments.

## Agent discovery

`/openapi.json`, `/llms.txt`, `/skill.md`, `/.well-known/x402.json`, `/.well-known/mcp.json`, `/.well-known/agent-card.json` and MCP discovery are public. `POST /mcp` accepts JSON-RPC discovery and four paid tools. A remote MCP connector provides transport; tool use still needs an x402-capable payment client or purchased session header.

Free `/v1/preview/*` routes expose coverage counts and observation health, not priced market records. `/health` distinguishes process health from feed freshness; `/version` exposes the deployed revision.

The Cloudflare Worker proxies every route once to the canonical backend. It has no separate payment engine, free tool quota, synthetic feed or session-minting authority. Publish the Worker separately with the authenticated official Wrangler CLI. A Render deployment does not update Cloudflare.

## Verification

Run `python -m pytest tests -q` and `node --test tests/worker.test.mjs`. Tests use isolated databases, deterministic unfunded cryptographic fixtures and mocked transports. They do not submit external payments. The feed engine also supports unsigned public-provider verification without buyer keys or transactions.
