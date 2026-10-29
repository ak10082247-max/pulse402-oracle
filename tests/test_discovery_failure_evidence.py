"""A missing feed must retain actual causes without blaming partial discovery."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from engine import base_dex, provider_reads
from engine.base_quotes import QuoteError


def unavailable_errors(code="DEXSCREENER_HTTP_429", status=429):
    return [{"symbol": symbol, "error": "DISCOVERY_UNAVAILABLE", "code": code, "http_status": status}
            for symbol in base_dex.TOKENS if symbol != "USDC"]


@pytest.mark.parametrize("code,status", [("DEXSCREENER_HTTP_429", 429), ("DEXSCREENER_TIMEOUT", None), ("DEXSCREENER_INVALID_SCHEMA", 200)])
def test_all_discovery_failure_keeps_actual_safe_cause_and_original_provider_diagnostics(monkeypatch, code, status):
    snapshot = {"pools": [], "errors": unavailable_errors(code, status)}
    engine = SimpleNamespace(snapshot=snapshot)
    async def acquire(_client): return engine
    monkeypatch.setattr(base_dex, "acquire_quote_cycle", acquire)
    monkeypatch.setattr(base_dex, "quote_freshness", lambda _engine: {})
    monkeypatch.setattr(provider_reads, "_DIAGNOSTICS", {})
    provider_reads.provider_error("dexscreener", code.removeprefix("DEXSCREENER_"), status=status)
    before = copy.deepcopy(provider_reads.provider_diagnostics())
    with pytest.raises(provider_reads.ProviderReadError) as caught:
        asyncio.run(base_dex.fetch_base_liquidity_state(None))
    assert caught.value.code == code and caught.value.provider_http_status == status
    assert provider_reads.provider_diagnostics() == before


@pytest.mark.parametrize("kind", ["partial_errors", "present_unrelated_pool", "unknown_vendor_code"])
def test_partial_discovery_or_untrusted_code_does_not_misattribute_quote_failure(monkeypatch, kind):
    errors = unavailable_errors()
    pools = []
    if kind == "partial_errors": errors.pop()
    elif kind == "present_unrelated_pool":
        pools = [{"baseToken": {"address": "0x" + "ab" * 20}, "quoteToken": {"address": base_dex.TOKENS["USDC"]}}]
    else: errors[0]["code"] = "DEXSCREENER_PRIVATE_VENDOR_MESSAGE"
    # Unknown codes from one row must not be used; leave no valid alternative.
    if kind == "unknown_vendor_code":
        for error in errors: error["code"] = "DEXSCREENER_PRIVATE_VENDOR_MESSAGE"
    engine = SimpleNamespace(snapshot={"pools": pools, "errors": errors})
    async def acquire(_client): return engine
    monkeypatch.setattr(base_dex, "acquire_quote_cycle", acquire)
    monkeypatch.setattr(base_dex, "quote_freshness", lambda _engine: {})
    with pytest.raises(QuoteError) as caught:
        asyncio.run(base_dex.fetch_base_liquidity_state(None))
    assert caught.value.code == "QUOTE_ACQUISITION_FAILED"


def test_first_actual_discovery_error_is_preserved_when_every_symbol_is_unavailable(monkeypatch):
    errors = unavailable_errors()
    errors[0].update(code="DEXSCREENER_HTTP_503", http_status=503)
    engine = SimpleNamespace(snapshot={"pools": [], "errors": errors})
    async def acquire(_client): return engine
    monkeypatch.setattr(base_dex, "acquire_quote_cycle", acquire)
    monkeypatch.setattr(base_dex, "quote_freshness", lambda _engine: {})
    with pytest.raises(provider_reads.ProviderReadError) as caught:
        asyncio.run(base_dex.fetch_base_liquidity_state(None))
    assert caught.value.code == "DEXSCREENER_HTTP_503" and caught.value.provider_http_status == 503


def test_partial_discovery_loss_does_not_poison_an_asset_with_real_quote_results(monkeypatch):
    token, usdc = base_dex.TOKENS["AERO"], base_dex.TOKENS["USDC"]
    pool = {"chainId": "base", "pairAddress": "0x" + "12" * 20, "dexId": "aerodrome",
            "baseToken": {"address": token}, "quoteToken": {"address": usdc}, "liquidity": {"usd": 123}}
    async def paths(tin, tout, raw):
        assert (tin, tout) == (usdc, token)
        return [{"amount_out_raw": str(raw * 2), "legs": []}]
    async def call(_address, signature, *args):
        assert signature == "balanceOf(address)"
        return (1000,)
    engine = SimpleNamespace(
        snapshot={"pools": [pool], "errors": [error for error in unavailable_errors() if error["symbol"] != "AERO"], "timestamp_ms": 100000},
        decimals={token: 18, usdc: 6}, paths=paths, call=call, observed_at_ms=100000,
        block="0x64", block_info={"hash": "0x" + "ab" * 32}, selected_provider="offline",
    )
    async def acquire(_client): return engine
    monkeypatch.setattr(base_dex, "acquire_quote_cycle", acquire)
    monkeypatch.setattr(base_dex, "quote_freshness", lambda _engine: {})
    result = asyncio.run(base_dex.fetch_base_liquidity_state(None))
    assert len(result) == 1 and result[0]["symbol"] == "AERO"
    assert [row["notional_usdc"] for row in result[0]["size_quotes_usdc"]] == ["100", "1000", "10000"]
    assert result[0]["timestamp_ms"] == 100000 and result[0]["liquidity_usd"] == 123
