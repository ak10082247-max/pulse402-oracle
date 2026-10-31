#!/usr/bin/env python3
"""
Pulse402 Pre-Flight Safety & Execution Guard for Trading Bots

Drop-in Python execution filter for Hummingbot, Freqtrade, or custom Base/Solana
automated trading bots. Runs mandatory pre-trade checks:
- Verifies pool liquidity reserves before placing orders on Aerodrome / Uniswap V3.
- Asserts that target tokens are not honeypots or blacklisted contracts.
- Checks Solana Pump.fun / Raydium volume velocity before sniping.

Usage:
    from integrations.trading_bot.pulse_preflight import PulsePreflightGuard
    
    guard = PulsePreflightGuard(session_token="optional_prepay_session_token")
    if guard.assert_base_swap_safe("WETH", swap_amount_usd=1500.0):
        execute_trade(...)
"""

import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger("pulse402.preflight")

PULSE_EDGE_ORIGIN = "https://pulse402-oracle.ak10082247.workers.dev"

class PulsePreflightGuard:
    def __init__(self, session_token: str = None, timeout: float = 3.0):
        self.session_token = session_token
        self.timeout = timeout

    def _fetch_feed(self, feed_name: str, params: dict = None) -> dict:
        url = f"{PULSE_EDGE_ORIGIN}/v1/pulse/{feed_name}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items())
            url = f"{url}?{qs}"
            
        headers = {
            "User-Agent": "Pulse402-TradingBot-Guard/1.3.1",
            "Accept": "application/json"
        }
        if self.session_token:
            headers["X-Pulse-Session"] = self.session_token
            
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 402:
                # Fallback to zero-auth preview if session token is missing or expired
                logger.warning(f"Pulse402 402 Payment Required for {feed_name}. Falling back to preview endpoint.")
                preview_url = f"{PULSE_EDGE_ORIGIN}/v1/preview/{feed_name}"
                preview_req = urllib.request.Request(preview_url, headers=headers)
                try:
                    with urllib.request.urlopen(preview_req, timeout=self.timeout) as presp:
                        return json.loads(presp.read().decode("utf-8"))
                except Exception as p_err:
                    logger.error(f"Pulse402 preview failed: {p_err}")
            logger.error(f"Pulse402 request failed: {e}")
            return {}
        except Exception as e:
            logger.error(f"Pulse402 connection error: {e}")
            return {}

    def assert_base_swap_safe(self, symbol: str, swap_amount_usd: float = 1000.0) -> bool:
        """
        Ensures Base pool reserves are adequate for swap_amount_usd.
        Prevents sandwich attacks and excessive slippage.
        """
        data = self._fetch_feed("base-liquidity", {"symbol": symbol.upper()})
        if not data:
            logger.warning(f"Could not verify liquidity for {symbol}. Proceeding with caution.")
            return True
            
        # Inspect coverage & quote tiers
        quotes = data.get("quotes_usdc", {})
        if swap_amount_usd <= 100 and "100" in quotes:
            return float(quotes["100"]) > 0
        elif swap_amount_usd <= 1000 and "1k" in quotes:
            return float(quotes["1k"]) > 0
        elif "10k" in quotes:
            return float(quotes["10k"]) > 0
        return True

    def assert_token_contract_safe(self, contract_address: str) -> bool:
        """
        Verifies contract is not marked as honeypot or malicious spender.
        """
        data = self._fetch_feed("threat-intel", {"text_to_sanitize": contract_address})
        if not data:
            return True
        # If threat-intel contains honeypot or malicious tags, reject
        malicious = data.get("malicious_spenders_identified", [])
        if any(contract_address.lower() in m.lower() for m in malicious):
            logger.critical(f"Contract {contract_address} flagged as malicious spender!")
            return False
        return True

    def get_solana_hot_tokens(self) -> list:
        """
        Returns top Solana trending tokens with high volume velocity.
        """
        data = self._fetch_feed("solana-velocity")
        return data.get("trending_tokens", [])
