"""Real-shape unfunded RPC fixtures; no provider, key, payment or broadcast."""
import asyncio
import copy
from types import SimpleNamespace

import pytest

from engine import base_dex, payments, poller
from engine.base_quotes import QuoteError


def fake_cycle(markets=()):
    usdc = base_dex.TOKENS["USDC"]
    assets = [(symbol, token) for symbol, token in base_dex.TOKENS.items() if symbol != "USDC"]
    pools = {symbol: "0x" + format(index + 1, "040x") for index, (symbol, _token) in enumerate(assets)}
    metadata = {pools[symbol]: {"token0": usdc, "token1": token, "fee": 500} for symbol, token in assets}
    recorded, balances = [], []
    failures = {"quote": None, "balance": None, "path": None}

    async def paths(tin, tout, raw):
        recorded.append((tin, tout, raw))
        symbol = next(symbol for symbol, token in assets if token == tout)
        if failures["quote"] and failures["quote"](symbol, raw):
            return []
        out = raw * 2 * 10**12
        leg = {"pool": pools[symbol], "dex": "uniswap", "method": "uniswap_v3_quoter_v2", "fee_ppm": 500,
               "token_in": tin, "token_out": tout, "amount_in_raw": str(raw), "amount_out_raw": str(out)}
        route = {"amount_in_raw": str(raw), "amount_out_raw": str(out), "legs": [leg]}
        if failures["path"]:
            failures["path"](symbol, route)
        return [route]

    async def call(token, method, types, values):
        assert method == "balanceOf(address)" and types == ("address",)
        pool, = values
        assert token in (metadata[pool]["token0"], metadata[pool]["token1"])
        balances.append((token, pool))
        if failures["balance"]:
            value = failures["balance"](token, pool)
            if isinstance(value, Exception):
                raise value
            if value is not False:
                return value
        return (123456789,)

    engine = SimpleNamespace(observed_at_ms=95000, block="0x64", block_info={"number": "0x64", "hash": "0x" + "ab" * 32, "timestamp": hex(98)},
        selected_provider="unfunded-test-provider", snapshot={"timestamp_ms": 1000, "pools": list(markets), "errors": [{"symbol": s, "code": "DEXSCREENER_HTTP_429", "http_status": 429} for s, _ in assets]},
        decimals={**{token: 18 for _symbol, token in assets}, usdc: 6}, paths=paths, call=call, pool_metadata=metadata, canonical_pools=set(metadata))
    return engine, recorded, balances, failures, pools


def install(monkeypatch, engine):
    async def acquire(_client):
        return engine
    monkeypatch.setattr(base_dex, "acquire_quote_cycle", acquire)
    monkeypatch.setattr(base_dex.time, "time", lambda: 100.0)
    monkeypatch.setattr(base_dex, "_BALANCE_HISTORY", {})


def test_hard_429_still_acquires_all_four_assets_all_three_sizes_and_two_real_balances(monkeypatch):
    engine, calls, balances, _failures, pools = fake_cycle()
    install(monkeypatch, engine)
    result = asyncio.run(base_dex.fetch_base_liquidity_state(None))
    assert len(result) == 4
    usdc = base_dex.TOKENS["USDC"]
    assert len(calls) == 16 and len(balances) == 8
    for row in result:
        token = row["token_address"]
        assert [raw for tin, tout, raw in calls if tout == token] == [10**6, 100*10**6, 1000*10**6, 10000*10**6]
        assert [quote["notional_usdc"] for quote in row["size_quotes_usdc"]] == ["100", "1000", "10000"]
        assert all(quote["route"]["amount_in_raw"] == str(int(quote["notional_usdc"]) * 10**6) for quote in row["size_quotes_usdc"])
        assert row["pool_token_balances_raw"] == {usdc: "123456789", token: "123456789"}
        assert row["pair_address"] == pools[row["symbol"]]
        assert row["primary_dex"] is None and row["selected_dex"] == "uniswap"
        for field in ("price_usd", "liquidity_usd", "volume_24h_usd", "price_delta_5m_pct", "netflow_velocity_60s_usd"):
            assert row[field] is None
        assert row["timestamp_ms"] == 95000  # failed market's ancient1000 is never reused
        evidence = row["evidence"]
        assert evidence["market_acquired_at_ms"] is None and evidence["market_provider"] is None
        assert evidence["market_discovery"] == {"status": "unavailable", "failure_code": "DEXSCREENER_HTTP_429", "provider_http_status": 429}
        assert evidence["onchain_core_status"] == "complete" and evidence["catalog_coverage"]["complete_catalog"] is True
        assert "not a global primary" in evidence["pool_selection"]
        assert row["pool_balance_change"]["status"] == "warming_up"


def market(engine, symbol):
    token, usdc = base_dex.TOKENS[symbol], base_dex.TOKENS["USDC"]
    pool = next(p for p, meta in engine.pool_metadata.items() if meta["token1"] == token)
    return {"pairAddress": pool, "baseToken": {"address": token}, "quoteToken": {"address": usdc}, "dexId": "uniswap", "priceUsd": "2.50", "liquidity": {"usd": 12345}, "volume": {"h24": 6789}, "priceChange": {"m5": 1.2}}


def test_normal_market_values_and_original_timestamps_are_preserved(monkeypatch):
    engine, *_ = fake_cycle()
    engine.snapshot.update(timestamp_ms=94000, pools=[market(engine, symbol) for symbol in base_dex.TOKENS if symbol != "USDC"], errors=[])
    install(monkeypatch, engine)
    rows = asyncio.run(base_dex.fetch_base_liquidity_state(None))
    assert len(rows) == 4
    for row in rows:
        assert row["price_usd"] == 2.5 and row["liquidity_usd"] == 12345 and row["volume_24h_usd"] == 6789
        assert row["price_delta_5m_pct"] == 1.2 and row["primary_dex"] == "uniswap"
        assert row["timestamp_ms"] == 94000 and row["evidence"]["market_acquired_at_ms"] == 94000
        assert row["evidence"]["market_discovery"]["status"] == "acquired"


def test_partial_market_failure_keeps_successful_metrics_and_independently_acquires_missing_assets(monkeypatch):
    engine, *_ = fake_cycle()
    engine.snapshot.update(timestamp_ms=94000, pools=[market(engine, "WETH")])
    install(monkeypatch, engine)
    rows = asyncio.run(base_dex.fetch_base_liquidity_state(None))
    assert len(rows) == 4
    weth = next(row for row in rows if row["symbol"] == "WETH")
    assert weth["liquidity_usd"] == 12345 and weth["timestamp_ms"] == 94000
    for row in rows:
        if row["symbol"] != "WETH":
            assert row["price_usd"] is None and row["timestamp_ms"] == 95000


@pytest.mark.parametrize("mutate", [
    lambda engine, route: engine.canonical_pools.clear(),
    lambda engine, route: route["legs"][0].update(token_out=base_dex.TOKENS["USDC"]),
    lambda engine, route: route["legs"][0].update(fee_ppm=3000),
    lambda engine, route: route["legs"][0].update(dex="unproved"),
    lambda engine, route: route.update(amount_in_raw="1"),
    lambda engine, route: route["legs"][0].update(amount_in_raw="1"),
    lambda engine, route: route["legs"][0].update(amount_out_raw="0"),
    lambda engine, route: route["legs"][0].update(amount_out_raw=True),
    lambda engine, route: route.update(amount_out_raw="1"),
])
def test_untrusted_or_amount_mismatched_paths_never_produce_positive_records(monkeypatch, mutate):
    engine, _calls, _balances, failures, _pools = fake_cycle()
    engine.snapshot["errors"] = []
    failures["path"] = lambda _symbol, route: mutate(engine, route)
    install(monkeypatch, engine)
    with pytest.raises(QuoteError):
        asyncio.run(base_dex.fetch_base_liquidity_state(None))


@pytest.mark.parametrize("bad_balance", [(None,), (True,), (-1,), (), None, QuoteError("RPC_UNAVAILABLE", "unfunded fixture")])
def test_missing_or_invalid_balance_cannot_be_sold_as_complete_positive(monkeypatch, bad_balance):
    engine, _calls, _balances, failures, _pools = fake_cycle()
    engine.snapshot["errors"] = []
    failures["balance"] = lambda _token, _pool: bad_balance
    install(monkeypatch, engine)
    with pytest.raises(QuoteError):
        asyncio.run(base_dex.fetch_base_liquidity_state(None))


@pytest.mark.parametrize("missing_size", [100, 1000, 10000])
def test_one_missing_offered_size_cannot_be_sold_as_complete_positive(monkeypatch, missing_size):
    engine, _calls, _balances, failures, _pools = fake_cycle()
    engine.snapshot["errors"] = []
    failures["quote"] = lambda _symbol, raw: raw == missing_size * 10**6
    install(monkeypatch, engine)
    with pytest.raises(QuoteError):
        asyncio.run(base_dex.fetch_base_liquidity_state(None))


def test_partial_quote_failure_is_disclosed_and_does_not_poison_another_complete_asset(monkeypatch):
    engine, _calls, _balances, failures, _pools = fake_cycle()
    failures["quote"] = lambda symbol, raw: symbol == "AERO" and raw == 1000 * 10**6
    install(monkeypatch, engine)
    rows = asyncio.run(base_dex.fetch_base_liquidity_state(None))
    assert {row["symbol"] for row in rows} == {"WETH", "DEGEN", "VIRTUAL"}
    coverage = rows[0]["evidence"]["catalog_coverage"]
    assert coverage["unavailable_assets"] == ["AERO"] and coverage["complete_catalog"] is False


def test_balance_reads_finishing_after_original_age_limit_do_not_retimestamp_or_return_positive(monkeypatch):
    engine, _calls, _balances, _failures, _pools = fake_cycle()
    install(monkeypatch, engine)
    now = [100.0]
    monkeypatch.setattr(base_dex.time, "time", lambda: now[0])
    original = engine.call
    async def slow(*args):
        result = await original(*args)
        now[0] = 105.0  # original95000 is nowexpired
        return result
    engine.call = slow
    with pytest.raises(QuoteError, match="expired"):
        asyncio.run(base_dex.fetch_base_liquidity_state(None))
    assert engine.observed_at_ms == 95000


def test_real_balance_history_warms_up_then_measures_same_pool_and_token_set(monkeypatch):
    engine, *_ = fake_cycle()
    install(monkeypatch, engine)
    first = asyncio.run(base_dex.fetch_base_liquidity_state(None))
    engine.observed_at_ms = 156000
    engine.block_info["timestamp"] = hex(156)
    monkeypatch.setattr(base_dex.time, "time", lambda: 157.0)
    second = asyncio.run(base_dex.fetch_base_liquidity_state(None))
    assert first[0]["pool_balance_change"]["status"] == "warming_up"
    assert second[0]["pool_balance_change"]["status"] == "measured"
    assert second[0]["pool_balance_change"]["observed_window_seconds"] == 61
    assert set(second[0]["pool_balance_change"]["delta_raw"].values()) == {"0"}
    assert "not attributed netflow" in second[0]["pool_balance_change"]["scope"]


@pytest.mark.parametrize("failure", ["no_quotes", "no_balances", "untrusted_pool"])
def test_real_purchase_pipeline_does_not_settle_when_onchain_core_cannot_be_acquired(monkeypatch, tmp_path, failure):
    from test_payments import base_payment
    engine, _calls, _balances, failures, _pools = fake_cycle()
    engine.snapshot["errors"] = []
    if failure == "no_quotes":
        failures["quote"] = lambda _symbol, _raw: True
    elif failure == "no_balances":
        failures["balance"] = lambda _token, _pool: (None,)
    else:
        engine.canonical_pools.clear()
    install(monkeypatch, engine)
    # The only signature is the existing unfunded fixture, with a mocked clock.
    monkeypatch.setattr(base_dex, "quote_freshness", lambda _engine: {})
    monkeypatch.setenv("PULSE_PAYMENTS_DB", str(tmp_path / "payments.sqlite3"))
    calls = []
    async def facilitator(action, _envelope):
        calls.append(action)
        assert action == "verify", "An unfulfilled core result must never settle"
        return {"isValid": True}
    monkeypatch.setattr(payments, "facilitator", facilitator)
    async def acquire():
        return await base_dex.fetch_base_liquidity_state(None)
    async def run():
        with pytest.raises(payments.PaymentError) as error:
            await payments.execute_purchase(payments.canonical(base_payment(nonce=991)), "/v1/pulse/base-liquidity", {}, acquire)
        assert error.value.status == 503
    asyncio.run(run())
    assert calls == ["verify"]
