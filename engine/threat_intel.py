"""Acquired provider token-risk evidence and a scoped local text sanitizer."""

import hashlib
import json
import re
import time
import unicodedata
from decimal import Decimal
import httpx
from .base_dex import TOKENS

INVISIBLE_CHARS = re.compile(
    r"[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufe00-\ufe0f\ufeff]"
)
ROLE_DELIMITERS = re.compile(
    r"\[/?(?:INST|SYSTEM|CONTEXT|RULES|PROMPT|DEVELOPER)\]|<\|(?:im_start|im_end|system|developer|assistant|user)\|?>",
    re.I,
)
INSTRUCTION_OVERRIDE = re.compile(
    r"\b(?:ignore|disregard|forget|override|bypass|supersede)\s+(?:(?:all|any|the|your|previous|prior|system|developer|earlier)\s+)*(?:instructions?|prompts?|rules?|directives?)\b",
    re.I,
)
WALLET_DRAIN_COMMAND = re.compile(
    r"\b(?:send|transfer|drain|withdraw|sweep|approve)\s*(?:(?:all|every|the|my|your)\s*)*(?:eth|usdc|tokens?|funds?|balance|wallet|treasury)\b",
    re.I,
)


def sanitize_untrusted_text(text, strict_mode=True):
    if not isinstance(text, str) or not isinstance(strict_mode, bool):
        raise ValueError("content must be text and strict_mode a boolean")
    clean = INVISIBLE_CHARS.sub("", text)
    normalized = unicodedata.normalize("NFKC", clean)
    threats = []
    if clean != text:
        threats.append("INVISIBLE_CONTROL_CHARACTERS")
    for expression, name in (
        (ROLE_DELIMITERS, "ROLE_DELIMITER_INJECTION"),
        (INSTRUCTION_OVERRIDE, "INSTRUCTION_OVERRIDE_ATTACK"),
        (WALLET_DRAIN_COMMAND, "WALLET_DRAIN_EXPLOIT_CALL"),
    ):
        if expression.search(normalized):
            threats.append(name)
    blocked = strict_mode and any(
        threat != "INVISIBLE_CONTROL_CHARACTERS" for threat in threats
    )
    return {
        "status": (
            "blocked"
            if blocked
            else (
                "sanitized"
                if threats and strict_mode
                else "flagged" if threats else "clean"
            )
        ),
        "threats_detected": threats,
        "is_safe": not threats,
        "sanitized_content": "" if blocked else clean if strict_mode else text,
        "scope": "Deterministic text patterns; not complete injection prevention or on-chain approval verification",
    }


def _tax(value):
    try:
        number = Decimal(str(value))
        return (
            float(number * 10000) if number.is_finite() and 0 <= number <= 1 else None
        )
    except Exception:
        return None


async def fetch_threat_intel_state(client=None):
    if client is None:
        async with httpx.AsyncClient(timeout=8) as owned:
            return await fetch_threat_intel_state(owned)
    addresses = [value for symbol, value in TOKENS.items() if symbol != "USDC"]
    response = await client.get(
        "https://api.gopluslabs.io/api/v1/token_security/8453",
        params={"contract_addresses": ",".join(addresses)},
        timeout=8,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("code") != 1 or not isinstance(body.get("result"), dict):
        raise ValueError("Token risk provider did not return acquired evidence")
    acquired_at = int(time.time() * 1000)
    rows = []
    for address, data in body["result"].items():
        if address.lower() not in addresses or not isinstance(data, dict):
            continue
        flag = data.get("is_honeypot")
        rows.append(
            {
                "token_address": address.lower(),
                "symbol": str(data.get("token_symbol") or ""),
                "chain": "base",
                "provider_honeypot_flag": (
                    True if flag == "1" else False if flag == "0" else None
                ),
                "buy_tax_bps": _tax(data.get("buy_tax")),
                "sell_tax_bps": _tax(data.get("sell_tax")),
                "cannot_sell_all": (
                    True
                    if data.get("cannot_sell_all") == "1"
                    else False if data.get("cannot_sell_all") == "0" else None
                ),
                "observed_at_ms": acquired_at,
                "evidence": {
                    "provider": "GoPlus Token Security",
                    "network": "eip155:8453",
                    "requested_address": address.lower(),
                    "response_sha256": hashlib.sha256(
                        json.dumps(data, sort_keys=True).encode()
                    ).hexdigest(),
                    "scope": "Provider-reported risk assessment; Pulse did not independently simulate a swap",
                },
            }
        )
    if not rows:
        raise ValueError("Provider returned no requested token evidence")
    return {
        "rolling_honeypots": [
            row for row in rows if row["provider_honeypot_flag"] is True
        ],
        "monitored_token_assessments": rows,
        "active_malicious_spenders": [],
        "spender_reputation_status": "No verified spender feed configured; empty list is not an all-clear",
        "coverage": {
            "requested_tokens": len(addresses),
            "returned_tokens": len(rows),
            "market_wide": False,
        },
        "sanitizer_engine": {
            "status": "active",
            "supported_checks": [
                "instruction_override",
                "role_delimiter_hijack",
                "invisible_unicode_concealment",
                "wallet_drain_text_commands",
            ],
            "guarantee": "scoped pattern detection only",
        },
        "timestamp_ms": acquired_at,
        "ttl_seconds": 10,
    }
