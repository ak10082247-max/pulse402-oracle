# Pulse402 MCP & x402 V2 Benchmark Suite

Official benchmarking and compliance test suite for Model Context Protocol (MCP) clients, AI agent frameworks, and autonomous trading runtimes implementing dual-chain (`eip155:8453` Base and `solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp`) x402 V2 micropayments.

---

## What This Suite Tests

1. **Protocol Handshake & Latency**:
   - `initialize` (JSON-RPC 2.0 protocol negotiation)
   - `tools/list` (tool capability schema discovery)
2. **Dual-Chain x402 V2 Challenge Negotiation**:
   - `HTTP 402 Payment Required` challenge parsing (`PAYMENT-REQUIRED` header & JSON-RPC body)
   - Dual-chain accepts verification (Circle EIP-3009 on Base, SPL TokenChecked on Solana)
3. **Execution Latency & Throughput**:
   - Per-call settlement response latency
   - High-Frequency Trading (HFT) prepay session pass latency (POST `/v1/pass/prepay`)
   - Burst requests per second (RPS) and p50/p95/p99 latency

---

## Quickstart

### Run the Performance Benchmark
```bash
python benchmarks/mcp_payment_benchmark.py --endpoint https://pulse402-oracle.ak10082247.workers.dev/mcp --iterations 20
```

### Run the CI/CD Compliance Test
```bash
python benchmarks/ci_agent_test.py
```

### Agent Evaluation Dataset
Agent evaluation tasks are defined in `benchmarks/fixtures/mcp_eval_tasks.json`. Compatible with SWE-bench, AgentBench, and GAIA evaluation harnesses.

---

## Integration in CI/CD (GitHub Actions)

Add this step to your agent framework workflow:
```yaml
- name: Verify Live x402 MCP Payment Oracle
  run: |
    python benchmarks/ci_agent_test.py
```
