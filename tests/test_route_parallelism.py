"""Deterministic dependency barriers prove parallelism without timing benchmarks."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from engine import arb_router, base_dex


def leg(pool, output):
    return {"pool": pool, "amount_out_raw": str(output)}


def path(pool, output):
    return {"amount_out_raw": str(output), "legs": [leg(pool, output)]}


def test_two_hop_queries_run_independently_preserving_amounts_limits_ties_and_pool_filter():
    async def run():
        engine = base_dex.CycleQuotes(None, {"pools": []})
        tin, tout, weth = (base_dex.TOKENS[key] for key in ("USDC", "AERO", "WETH"))
        initial_started, reverse_started, calls = [], [], []
        initial_ready, reverse_ready, second_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        direct = [leg("direct-one", 1000), leg("direct-two", 1000)]
        first = [leg("first-a", 111), leg("first-b", 222), leg("unused-first", 333)]
        seconds = {111: [leg("first-a", 9000), leg("second-a", 500), leg("unused-second", 80000)], 222: [leg("second-b", 1000), leg("second-c", 700), leg("unused-second", 80000)]}
        original = deepcopy((direct, first, seconds))

        async def quotes(start, end, amount):
            calls.append((start, end, amount))
            if start == tin:
                initial_started.append(end)
                if len(initial_started) == 2: initial_ready.set()
                await initial_ready.wait()  # Serial direct/first-leg acquisition cannot pass.
                return direct if end == tout else first
            assert start == weth and end == tout and amount in (111, 222)
            reverse_started.append(amount)
            if len(reverse_started) == 2: reverse_ready.set()
            await reverse_ready.wait()  # Serial second-leg acquisition cannot pass.
            if amount == 111: await second_finished.wait()
            else: second_finished.set()
            return seconds[amount]

        engine.direct_quotes = quotes
        result = await asyncio.wait_for(engine._paths(tin, tout, 101), 2)
        assert calls == [(tin, tout, 101), (tin, weth, 101), (weth, tout, 111), (weth, tout, 222)]
        assert [row["amount_out_raw"] for row in result] == ["1000", "1000", "1000", "700", "500"]
        assert [row["legs"][-1]["pool"] for row in result] == ["direct-one", "direct-two", "second-b", "second-c", "second-a"]
        assert all(row["amount_in_raw"] == "101" for row in result)
        assert result[2]["legs"] == [first[1], seconds[222][0]]
        assert result[-1]["legs"] == [first[0], seconds[111][1]]
        assert (direct, first, seconds) == original
    asyncio.run(run())


def test_weth_pair_uses_only_original_direct_quotes():
    async def run():
        engine = base_dex.CycleQuotes(None, {"pools": []})
        tin, tout = base_dex.TOKENS["USDC"], base_dex.TOKENS["WETH"]
        calls = []
        async def quotes(start, end, amount):
            calls.append((start, end, amount))
            return [leg("direct", 123)]
        engine.direct_quotes = quotes
        result = await engine._paths(tin, tout, 101)
        assert calls == [(tin, tout, 101)]
        assert result == [{"amount_in_raw": "101", "amount_out_raw": "123", "legs": [leg("direct", 123)]}]
    asyncio.run(run())


def test_arbitrage_reverse_quotes_start_together_and_preserve_exact_buy_correspondence(monkeypatch):
    async def run():
        tin, tout = base_dex.TOKENS["AERO"], base_dex.TOKENS["USDC"]
        buys = [path("buy-a", 111), path("buy-b", 222), path("buy-c", 333), path("unused-buy", 444)]
        sells = {111: [path("buy-a", 99999), path("sell-a", 110), path("sell-b", 105), path("unused-sell", 99999)], 222: [path("sell-c", 110), path("buy-b", 99999), path("sell-d", 90)], 333: [path("sell-e", 120), path("sell-f", 110), path("sell-g", 80)]}
        original = deepcopy((buys, sells))
        calls, reverse_started, completion_order = [], [], []
        ready, third_done, second_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def paths(start, end, amount):
            calls.append((start, end, amount))
            if (start, end) == (tin, tout):
                assert amount == 100
                return buys
            assert (start, end) == (tout, tin) and amount in (111, 222, 333)
            reverse_started.append(amount)
            if len(reverse_started) == 3: ready.set()
            await ready.wait()  # All three reverse requests must begin before any finishes.
            if amount == 111: await second_done.wait()
            elif amount == 222: await third_done.wait()
            completion_order.append(amount)
            if amount == 333: third_done.set()
            elif amount == 222: second_done.set()
            return sells[amount]
        engine = SimpleNamespace(paths=paths, decimals={tin: 2, tout: 6}, observed_at_ms=95000, block="0x64", block_info={"hash": "0x" + "ab" * 32})
        async def acquire(_client): return engine
        monkeypatch.setattr(arb_router, "acquire_quote_cycle", acquire)
        monkeypatch.setattr(arb_router, "quote_freshness", lambda value: {"quote_observed_at_ms": value.observed_at_ms})
        monkeypatch.setattr(arb_router, "ARB_PAIRS", [("AERO", "USDC", "1")])
        rows = await asyncio.wait_for(arb_router.fetch_arb_routes_state(None), 2)
        assert completion_order == [333, 222, 111]
        assert calls == [(tin, tout, 100), (tout, tin, 111), (tout, tin, 222), (tout, tin, 333)]
        assert len(rows) == 1 and rows[0]["timestamp_ms"] == 95000
        assert rows[0]["acquired_buy_quotes"] == buys  # Includes the original fourth acquisition.
        routes = rows[0]["routes"]
        assert [route["round_trip_output_raw"] for route in routes] == ["120", "110", "110", "110", "105", "90", "80"]
        assert [route["buy"]["legs"][0]["pool"] for route in routes] == ["buy-c", "buy-a", "buy-b", "buy-c", "buy-a", "buy-b", "buy-c"]
        assert [route["gross_profit_raw"] for route in routes] == ["20", "10", "10", "10", "5", "-10", "-20"]
        assert all(route["net_profit_usd"] is None and route["is_profitable"] is None for route in routes)
        assert (buys, sells) == original
    asyncio.run(run())


def test_duplicate_exact_reverse_amounts_share_cache_but_keep_distinct_buy_associations(monkeypatch):
    async def run():
        tin, tout = base_dex.TOKENS["AERO"], base_dex.TOKENS["USDC"]
        engine = base_dex.CycleQuotes(None, {"pools": []})
        engine.decimals, engine.observed_at_ms, engine.block = {tin: 2, tout: 6}, 95000, "0x64"
        engine.block_info = {"hash": "0x" + "ab" * 32}
        calls = []
        async def acquired(start, end, amount):
            calls.append((start, end, amount))
            await asyncio.sleep(0)
            return [path("buy-a", 111), path("buy-b", 111)] if start == tin else [path("sell-a", 110)]
        engine._paths = acquired
        async def acquire(_client): return engine
        monkeypatch.setattr(arb_router, "acquire_quote_cycle", acquire)
        monkeypatch.setattr(arb_router, "quote_freshness", lambda _engine: {})
        monkeypatch.setattr(arb_router, "ARB_PAIRS", [("AERO", "USDC", "1")])
        rows = await arb_router.fetch_arb_routes_state(None)
        assert calls == [(tin, tout, 100), (tout, tin, 111)]
        assert [route["buy"]["legs"][0]["pool"] for route in rows[0]["routes"]] == ["buy-a", "buy-b"]
        assert len(engine.route_quotes) == 2
        await engine.close_pending()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_parallel_fanout_drains_owned_siblings_before_propagating_failure(failure):
    async def run():
        started, stopped = asyncio.Event(), asyncio.Event()
        async def blocked():
            started.set()
            try: await asyncio.Event().wait()
            finally: stopped.set()
        async def failed():
            await started.wait()
            if failure == "cancel": raise asyncio.CancelledError()
            raise ValueError("one independent read failed")
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else ValueError):
            await asyncio.wait_for(base_dex.gather_quote_reads(failed(), blocked()), 2)
        assert stopped.is_set()
    asyncio.run(run())
