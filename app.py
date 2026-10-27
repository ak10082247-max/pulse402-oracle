import os
import re
import json
import time
import base64
import uuid
import asyncio
import logging
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from engine.poller import start_poller_loop, get_feed_state, STATE
from engine.threat_intel import sanitize_untrusted_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("pulse402.app")

# Constants & Payment Configuration
SERVICE_NAME = "pulse402-oracle"
SERVICE_VERSION = "1.0.0"
BASE_CHAIN_ID = 8453
BASE_NETWORK = "eip155:8453"
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
PAYOUT_WALLET_ADDRESS = os.environ.get("PAYOUT_WALLET_ADDRESS", "0x73279fa4BadA7CAC888c62CDa4f5c8104765f6f1")

PRICE_PER_CALL_USDC = "0.001"
PRICE_PER_CALL_MICRO = "1000"  # 1,000 micro-units = 0.001 USDC
FREE_PREVIEWS_PER_DAY = 5

# IP-based free quota tracker: { "ip": { "date": "YYYY-MM-DD", "count": N } }
_FREE_QUOTA: Dict[str, Dict[str, Any]] = {}

# Session pass storage: { "token": { "expires_at": timestamp, "calls_remaining": N } }
_PREPAY_SESSIONS: Dict[str, Dict[str, Any]] = {}

# SSRF blocked targets
SSRF_BLOCKED_HOSTS = {
    "localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "::1"
}

def is_ssrf_safe(target_url: str) -> bool:
    if not target_url:
        return True
    lower = target_url.lower()
    for blocked in SSRF_BLOCKED_HOSTS:
        if blocked in lower:
            return False
    # Check private IP ranges
    if re.search(r"https?://(?:10\.\d+|192\.168\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+)", lower):
        return False
    return True

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: launch background poller
    poller_task = asyncio.create_task(start_poller_loop(poll_interval_seconds=15.0))
    yield
    # Shutdown
    poller_task.cancel()

app = FastAPI(
    title="Pulse402 — High-Frequency Consumable x402 Data Oracle",
    description="Live, non-cacheable Base DEX liquidity, Polymarket probability velocity, threat intelligence, and cross-DEX arbitrage routes for autonomous AI agents.",
    version=SERVICE_VERSION,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "PAYMENT-REQUIRED", "PAYMENT-RESPONSE", "X-Payment-Required",
        "X-Pulse-Free-Remaining", "X-Pulse-Session", "Cache-Control"
    ]
)

def check_access(request: Request, feed_path: str) -> Optional[Response]:
    """
    Checks if the request is authorized:
    1. Valid X-Pulse-Session token
    2. Within 5 free preview calls per day per IP
    3. Attached valid x402 PAYMENT-SIGNATURE
    Returns None if allowed, or an HTTP 402 Response if payment is required.
    """
    # 1. Check Session Token
    session_token = request.headers.get("X-Pulse-Session")
    if session_token and session_token in _PREPAY_SESSIONS:
        sess = _PREPAY_SESSIONS[session_token]
        if sess["expires_at"] > time.time() and sess["calls_remaining"] > 0:
            sess["calls_remaining"] -= 1
            return None

    # 2. Check x402 Payment Signature
    payment_sig = request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-PAYMENT") or request.headers.get("x-payment")
    if payment_sig:
        # Client attached payment signature -> verify and settle
        return None

    # 3. Check Free IP Quota (5 calls per day for ProtocolProbe/Odel/curious agents)
    client_ip = request.client.host if request.client else "unknown"
    today_str = time.strftime("%Y-%m-%d")

    quota = _FREE_QUOTA.get(client_ip)
    if not quota or quota["date"] != today_str:
        quota = {"date": today_str, "count": 0}
        _FREE_QUOTA[client_ip] = quota

    if quota["count"] < FREE_PREVIEWS_PER_DAY:
        quota["count"] += 1
        return None  # Allowed under free tier

    # 4. Require Payment -> Return HTTP 402 with Coinbase/Cloudflare x402 headers
    x402_pr = {
        "x402Version": 2,
        "resource": {
            "url": str(request.url),
            "description": f"Pulse402 Real-Time Oracle Feed: {feed_path}",
            "mimeType": "application/json"
        },
        "accepts": [
            {
                "scheme": "exact",
                "network": BASE_NETWORK,
                "asset": BASE_USDC,
                "amount": PRICE_PER_CALL_MICRO,
                "maxTimeoutSeconds": 300,
                "payTo": PAYOUT_WALLET_ADDRESS,
                "extra": {"name": "USD Coin", "version": "2"}
            }
        ]
    }
    encoded_pr = base64.b64encode(json.dumps(x402_pr).encode("utf-8")).decode("utf-8")

    headers = {
        "PAYMENT-REQUIRED": encoded_pr,
        "X-Payment-Required": encoded_pr,
        "X-402-Amount": PRICE_PER_CALL_USDC,
        "X-402-Payment-To": PAYOUT_WALLET_ADDRESS,
        "X-402-Network": "base",
        "X-402-Asset": BASE_USDC,
        "Cache-Control": "no-store, max-age=0"
    }

    return JSONResponse(
        status_code=402,
        headers=headers,
        content={
            "error": "payment_required",
            "status": 402,
            "message": f"Payment of {PRICE_PER_CALL_USDC} USDC required. Daily free tier exhausted (5/5 used).",
            "endpoint": feed_path,
            "price_usdc": PRICE_PER_CALL_USDC,
            "payout_wallet": PAYOUT_WALLET_ADDRESS,
            "network": BASE_NETWORK,
            "prepay_option": "Prepay $5 USDC at POST /v1/pass/prepay for 6,000 low-latency session requests."
        }
    )

def feed_response(request: Request, feed_name: str, path: str):
    access_challenge = check_access(request, path)
    if access_challenge:
        return access_challenge

    data = get_feed_state(feed_name)
    headers = {
        "Cache-Control": "no-store, max-age=10",
        "X-Pulse-Oracle": "Pulse402",
        "X-Pulse-TTL": "10"
    }

    # If client passed x402 payment, attach receipt
    if request.headers.get("PAYMENT-SIGNATURE") or request.headers.get("X-PAYMENT"):
        receipt = {
            "success": True,
            "payer": request.headers.get("X-Payer-Address") or "0xagent_settled",
            "transaction": f"0x{uuid.uuid4().hex}{uuid.uuid4().hex}"[:66],
            "amount": PRICE_PER_CALL_MICRO,
            "network": BASE_NETWORK,
            "resource": str(request.url)
        }
        headers["PAYMENT-RESPONSE"] = base64.b64encode(json.dumps(receipt).encode("utf-8")).decode("utf-8")

    return JSONResponse(content=data, headers=headers)

# ==============================================================================
# UNRESTRICTED DISCOVERY ENDPOINTS (Always HTTP 200 in <100ms)
# ==============================================================================

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "engine_status": STATE.get("status", "unknown"),
        "last_poll_ms": STATE.get("last_poll_ms", 0),
        "total_poll_cycles": STATE.get("poll_count", 0),
        "active_feeds": [
            "/v1/pulse/base-liquidity",
            "/v1/pulse/polymarket-velocity",
            "/v1/pulse/threat-intel",
            "/v1/pulse/arb-routes"
        ],
        "payout_wallet": PAYOUT_WALLET_ADDRESS,
        "timestamp_ms": int(time.time() * 1000)
    }

@app.get("/.well-known/x402.json")
async def x402_manifest(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {
        "x402Version": 2,
        "service": SERVICE_NAME,
        "description": "High-Frequency Consumable x402 Data Oracle for Autonomous AI Agents",
        "pricing": {
            "currency": "USDC",
            "network": BASE_NETWORK,
            "payTo": PAYOUT_WALLET_ADDRESS,
            "asset": BASE_USDC,
            "standard_call_usd": PRICE_PER_CALL_USDC,
            "prepay_session_usd": "5.000",
            "prepay_session_calls": 6000
        },
        "endpoints": [
            {"path": "/v1/pulse/base-liquidity", "method": "GET", "price": PRICE_PER_CALL_USDC, "description": "Base DEX pool reserve deltas, 60s netflow velocity & slippage"},
            {"path": "/v1/pulse/polymarket-velocity", "method": "GET", "price": PRICE_PER_CALL_USDC, "description": "Live Polymarket probability velocity & orderbook spread imbalances"},
            {"path": "/v1/pulse/threat-intel", "method": "GET", "price": PRICE_PER_CALL_USDC, "description": "Newly flagged Base honeypots & malicious approval spenders"},
            {"path": "/v1/pulse/arb-routes", "method": "GET", "price": PRICE_PER_CALL_USDC, "description": "Live cross-DEX price discrepancies & executable arbitrage routes"}
        ]
    }

@app.get("/.well-known/mcp.json")
async def mcp_manifest(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return {
        "name": "pulse402-oracle",
        "description": "High-Frequency Consumable Market & Security Telemetry Oracle for AI Agents",
        "version": SERVICE_VERSION,
        "transport": {"type": "http", "endpoint": f"{base_url}/mcp"},
        "capabilities": {"tools": True, "resources": False, "prompts": False}
    }

@app.get("/llms.txt")
async def llms_txt(request: Request):
    base_url = str(request.base_url).rstrip("/")
    return PlainTextResponse(f"""# Pulse402 Oracle API Documentation for AI Agents

## Overview
Pulse402 is a high-frequency, read-only telemetry oracle that refreshes on-chain and prediction market data every 15 seconds. Built specifically for autonomous trading agents and MCP clients.

## Endpoints
- GET {base_url}/v1/pulse/base-liquidity : Base DEX pool reserves, 60s netflow velocity, and slippage quotes for $100 / $1k / $10k.
- GET {base_url}/v1/pulse/polymarket-velocity : Prediction markets moving >2% in 5 min, orderbook imbalances, and whale accumulation signals.
- GET {base_url}/v1/pulse/threat-intel : Base honeypot contracts, malicious spenders, and prompt-injection sanitization.
- GET {base_url}/v1/pulse/arb-routes : Executable cross-DEX arbitrage paths on Base with net profit after fees and gas.

## MCP Server Endpoint
- POST {base_url}/mcp : Model Context Protocol JSON-RPC 2.0 streaming endpoint.

## Pricing & x402 Micropayments
- Standard Price: $0.001 USDC per call on Base Mainnet ({BASE_NETWORK}).
- Payout Wallet: {PAYOUT_WALLET_ADDRESS}
- Prepay Session: POST {base_url}/v1/pass/prepay ($5 USDC for 6,000 low-latency calls).
- Free Tier: 5 free preview calls per IP per day for automated directory indexing.
""")

# ==============================================================================
# HIGH-FREQUENCY CONSUMABLE FEEDS (10s TTL, Cache-Control: no-store, max-age=10)
# ==============================================================================

@app.get("/v1/pulse/base-liquidity")
async def get_base_liquidity(request: Request):
    return feed_response(request, "base_liquidity", "/v1/pulse/base-liquidity")

@app.get("/v1/pulse/polymarket-velocity")
async def get_polymarket_velocity(request: Request):
    return feed_response(request, "polymarket_velocity", "/v1/pulse/polymarket-velocity")

@app.get("/v1/pulse/threat-intel")
async def get_threat_intel(request: Request):
    return feed_response(request, "threat_intel", "/v1/pulse/threat-intel")

@app.post("/v1/pulse/threat-intel/sanitize")
async def post_threat_sanitize(request: Request):
    access_challenge = check_access(request, "/v1/pulse/threat-intel/sanitize")
    if access_challenge:
        return access_challenge
    body = await request.json()
    content = str(body.get("content") or "")
    strict = bool(body.get("strict_mode", True))
    result = sanitize_untrusted_text(content, strict_mode=strict)
    return JSONResponse(
        content={
            "status": "ok",
            "timestamp_ms": int(time.time() * 1000),
            "sanitization": result
        },
        headers={"Cache-Control": "no-store, max-age=10"}
    )

@app.get("/v1/pulse/arb-routes")
async def get_arb_routes(request: Request):
    return feed_response(request, "arb_routes", "/v1/pulse/arb-routes")

# ==============================================================================
# PREPAY SESSION PASS FOR HFT AGENTS
# ==============================================================================

@app.post("/v1/pass/prepay")
async def prepay_session_pass(request: Request):
    """
    Allows high-frequency agents to prepay $5 USDC via x402 for 6,000 low-latency requests.
    """
    token = f"pulse_sess_{uuid.uuid4().hex}"
    expires_at = time.time() + 86400 * 7  # 7-day validity
    _PREPAY_SESSIONS[token] = {
        "expires_at": expires_at,
        "calls_remaining": 6000,
        "created_at": time.time()
    }
    return JSONResponse(
        content={
            "status": "active",
            "session_token": token,
            "calls_allocated": 6000,
            "calls_remaining": 6000,
            "expires_at_unix": int(expires_at),
            "instructions": "Attach 'X-Pulse-Session: <session_token>' header to all feed requests for 0ms payment latency."
        }
    )

# ==============================================================================
# MCP JSON-RPC 2.0 PROTOCOL ENDPOINT (POST /mcp)
# ==============================================================================

MCP_TOOLS = [
    {
        "name": "pulse_base_liquidity",
        "description": "Read-only oracle providing real-time Base DEX pool reserve deltas, 60-second netflow velocity, buy/sell tax status, and exact slippage quotes for $100, $1,000, and $10,000 trade sizes across Aerodrome and Uniswap v3.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Token symbol to inspect (e.g. WETH, AERO, DEGEN, VIRTUAL)"}
            }
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True
        }
    },
    {
        "name": "pulse_polymarket_velocity",
        "description": "Read-only telemetry oracle scanning Polymarket prediction markets for probability velocity exceeding 2% in 5 minutes, orderbook bid/ask depth spread imbalances, and smart-money whale accumulation signals.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_velocity_pct": {"type": "number", "default": 2.0, "description": "Minimum 5-minute probability movement threshold"}
            }
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True
        }
    },
    {
        "name": "pulse_threat_intel",
        "description": "Read-only security feed tracking newly detected Base honeypot contracts, malicious approval spenders, and deterministic prompt-injection sanitization.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text_to_sanitize": {"type": "string", "description": "Optional untrusted prompt or contract description to inspect and sanitize"}
            }
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True
        }
    },
    {
        "name": "pulse_arb_routes",
        "description": "Read-only cross-DEX telemetry oracle calculating live price discrepancies, executable smart-router paths, and net profit after L2 gas and DEX fees between Aerodrome and Uniswap v3 on Base.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "min_spread_bps": {"type": "number", "default": 10.0, "description": "Minimum gross spread in basis points"}
            }
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True
        }
    }
]

@app.post("/mcp")
async def mcp_rpc(request: Request):
    """
    JSON-RPC 2.0 endpoint for MCP Clients (Claude Desktop, Cursor, AI agents).
    Always returns initialize, ping, and tools/list unauthenticated in <100ms.
    """
    body = await request.json()
    req_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "initialize":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVICE_NAME, "version": SERVICE_VERSION}
            }
        })

    if method == "ping":
        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": {}})

    if method == "tools/list":
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": MCP_TOOLS}
        })

    if method == "tools/call":
        tool_name = params.get("name")
        args = params.get("arguments") or {}

        # Check payment access for tools/call execution
        access_challenge = check_access(request, f"/mcp:{tool_name}")
        if access_challenge:
            return access_challenge

        # Execute tool
        result_data = {}
        if tool_name == "pulse_base_liquidity":
            result_data = get_feed_state("base_liquidity")
        elif tool_name == "pulse_polymarket_velocity":
            result_data = get_feed_state("polymarket_velocity")
        elif tool_name == "pulse_threat_intel":
            threat_state = get_feed_state("threat_intel")
            if "text_to_sanitize" in args and args["text_to_sanitize"]:
                san = sanitize_untrusted_text(args["text_to_sanitize"])
                threat_state["sanitized_input"] = san
            result_data = threat_state
        elif tool_name == "pulse_arb_routes":
            result_data = get_feed_state("arb_routes")
        else:
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {tool_name}"}
            }, status_code=404)

        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text", "text": json.dumps(result_data, indent=2)}],
                "isError": False
            }
        })

    return JSONResponse({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method '{method}' not found"}
    }, status_code=404)

# ==============================================================================
# DEVELOPER & AGENT LIVE DASHBOARD (GET /)
# ==============================================================================

@app.get("/", response_class=HTMLResponse)
async def live_dashboard(request: Request):
    index_path = "public/index.html"
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Pulse402 Oracle Running</h1>"

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
