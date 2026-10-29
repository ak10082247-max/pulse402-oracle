"""Offline dependency barriers for pinned metadata and its trust boundaries."""
import asyncio

import httpx
import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from engine.base_quotes import BaseQuotes, QuoteError, TOKENS, UNI_FACTORY, UNI_QUOTER, AERO_FACTORY

USDC, WETH = TOKENS["USDC"].lower(), TOKENS["WETH"].lower()
POOL, OTHER = "0x" + "12" * 20, "0x" + "34" * 20
BLOCK, HASH = "0x64", "0x" + "ab" * 32
V3 = {"pool": POOL, "dex": "uniswap", "labels": ["v3"], "fee": 10000}
QUOTE = "quoteExactInputSingle((address,address,uint256,uint24,uint160))"


class MetadataBarrier:
    def __init__(self, expected, results):
        self.expected, self.results = expected, results
        self.started, self.finished = [], []
        self.ready = asyncio.Event()

    async def read(self, key):
        self.started.append(key)
        if len(self.started) == self.expected:
            self.ready.set()
        await self.ready.wait()  # Serial reads cannot reach this barrier.
        self.finished.append(key)
        result = self.results[key]
        if isinstance(result, BaseException):
            raise result
        return result


def pinned_engine():
    engine = BaseQuotes(None)
    engine.block, engine.block_info = BLOCK, {"hash": HASH}
    engine.decimals = {USDC: 6, WETH: 18}
    return engine


def test_setup_parallel_decimals_remain_after_exact_chain_and_block_validation():
    async def run():
        engine, order = BaseQuotes(None), []
        barrier = MetadataBarrier(2, {USDC: (6,), WETH: (18,)})
        async def rpc(method, params):
            order.append((method, params))
            return {"eth_chainId": "0x2105", "eth_blockNumber": BLOCK, "eth_getBlockByNumber": {"hash": HASH}}[method]
        async def call(address, signature, types=(), values=(), outputs=("uint256",)):
            assert order == [("eth_chainId", []), ("eth_blockNumber", []), ("eth_getBlockByNumber", [BLOCK, False])]
            assert engine.block == BLOCK and engine.block_info == {"hash": HASH}
            assert signature == "decimals()" and outputs == ("uint8",)
            return await barrier.read(address)
        engine.rpc, engine.call = rpc, call
        assert await asyncio.wait_for(engine.setup(USDC, WETH), 2) is engine
        assert engine.decimals == {USDC: 6, WETH: 18}
        assert set(barrier.finished) == {USDC, WETH}
    asyncio.run(run())


@pytest.mark.parametrize("chain,block_info,code", [("0x1", {"hash": HASH}, "WRONG_RPC_CHAIN"), ("0x2105", {}, "INVALID_BLOCK")])
def test_invalid_pin_never_starts_metadata(chain, block_info, code):
    async def run():
        engine = BaseQuotes(None)
        async def rpc(method, params):
            return {"eth_chainId": chain, "eth_blockNumber": BLOCK, "eth_getBlockByNumber": block_info}[method]
        async def forbidden(*args, **kwargs):
            pytest.fail("Metadata must not be read before chain/block validation")
        engine.rpc, engine.call = rpc, forbidden
        with pytest.raises(QuoteError) as caught:
            await engine.setup(USDC, WETH)
        assert caught.value.code == code
    asyncio.run(run())


@pytest.mark.parametrize("first_fails", [False, True])
def test_setup_drains_both_reads_and_preserves_first_error_and_partial_metadata(first_fails):
    async def run():
        engine = BaseQuotes(None)
        first, second = QuoteError("FIRST", "first"), QuoteError("SECOND", "second")
        barrier = MetadataBarrier(2, {USDC: first if first_fails else (6,), WETH: second})
        async def rpc(method, params):
            return {"eth_chainId": "0x2105", "eth_blockNumber": BLOCK, "eth_getBlockByNumber": {"hash": HASH}}[method]
        async def call(address, *args, **kwargs): return await barrier.read(address)
        engine.rpc, engine.call = rpc, call
        with pytest.raises(QuoteError) as caught:
            await asyncio.wait_for(engine.setup(USDC, WETH), 2)
        assert caught.value is (first if first_fails else second)
        assert set(barrier.finished) == {USDC, WETH}
        assert engine.decimals == ({} if first_fails else {USDC: 6})
    asyncio.run(run())


def test_v3_metadata_parallelism_requires_factory_proof_and_preserves_exact_quote_and_cache():
    async def run():
        engine, calls = pinned_engine(), []
        barrier = MetadataBarrier(3, {"token0()": (USDC.upper(),), "token1()": (WETH.upper(),), "fee()": (500,)})
        async def call(address, signature, types=(), values=(), outputs=("uint256",)):
            calls.append((address, signature, types, values, outputs))
            assert engine.block == BLOCK
            if signature in barrier.results:
                assert address == POOL
                return await barrier.read(signature)
            if address == UNI_FACTORY:
                assert set(barrier.finished) == set(barrier.results)
                assert engine.pool_metadata[POOL] == {"token0": USDC, "token1": WETH}
                assert signature == "getPool(address,address,uint24)" and values == (USDC, WETH, 500)
                return (POOL,)
            assert address == UNI_QUOTER and signature == QUOTE
            assert engine.pool_metadata[POOL]["fee"] == 500
            assert values == ((USDC, WETH, 1234567, 500, 0),)
            return (987654321, 444, 7, 123456)
        engine.call = call
        expected = {"dex": "uniswap", "pool": POOL, "token_in": USDC, "token_out": WETH,
                    "amount_in_raw": "1234567", "amount_out_raw": "987654321", "decimals_in": 6, "decimals_out": 18,
                    "lp_fee_included": True, "method": "uniswap_v3_quoter_v2", "fee_ppm": 500,
                    "sqrt_price_x96_after": "444", "initialized_ticks_crossed": 7, "gas_units_quote_estimate": 123456}
        assert await asyncio.wait_for(engine.quote(V3, USDC, WETH, 1234567), 2) == expected
        assert await engine.quote(V3, USDC, WETH, 1234567) == expected
        assert [item[1] for item in calls] == ["token0()", "token1()", "fee()", "getPool(address,address,uint24)", QUOTE, QUOTE]
    asyncio.run(run())


@pytest.mark.parametrize("failure,expected", [
    ("token0", "TOKEN0_REVERT"), ("token1", "TOKEN1_REVERT"), ("mismatch_and_fee", "POOL_TOKEN_MISMATCH"),
    ("fee", "FEE_REVERT"), ("factory_mismatch", "UNTRUSTED_POOL"), ("factory_revert", "FACTORY_REVERT"),
])
def test_failed_or_mismatched_metadata_never_becomes_trusted_or_reaches_quoter(failure, expected):
    async def run():
        engine, later = pinned_engine(), []
        values = {"token0()": (USDC,), "token1()": (WETH,), "fee()": (500,)}
        if failure == "token0":
            values.update({"token0()": QuoteError("TOKEN0_REVERT", "token0"), "token1()": QuoteError("TOKEN1_REVERT", "token1"), "fee()": QuoteError("FEE_REVERT", "fee")})
        elif failure == "token1":
            values.update({"token1()": QuoteError("TOKEN1_REVERT", "token1"), "fee()": QuoteError("FEE_REVERT", "fee")})
        elif failure == "mismatch_and_fee":
            values.update({"token1()": (OTHER,), "fee()": QuoteError("FEE_REVERT", "fee")})
        elif failure == "fee": values["fee()"] = QuoteError("FEE_REVERT", "fee")
        barrier = MetadataBarrier(3, values)
        async def call(address, signature, *args, **kwargs):
            if signature in values: return await barrier.read(signature)
            later.append((address, signature))
            assert address == UNI_FACTORY
            if failure == "factory_revert": raise QuoteError("FACTORY_REVERT", "factory")
            return (OTHER,)
        engine.call = call
        with pytest.raises(QuoteError) as caught:
            await asyncio.wait_for(engine.quote(V3, USDC, WETH, 123), 2)
        assert caught.value.code == expected
        assert set(barrier.finished) == set(values)
        assert "fee" not in engine.pool_metadata.get(POOL, {})
        assert later == ([(UNI_FACTORY, "getPool(address,address,uint24)")] if failure.startswith("factory_") else [])
        if failure in ("token0", "token1"): assert POOL not in engine.pool_metadata
    asyncio.run(run())


def test_aerodrome_parallel_token_reads_keep_registration_fee_and_amount_dependencies():
    async def run():
        engine, serial = pinned_engine(), []
        barrier = MetadataBarrier(2, {"token0()": (USDC,), "token1()": (WETH,)})
        async def call(address, signature, types=(), values=(), outputs=("uint256",)):
            if signature in barrier.results: return await barrier.read(signature)
            serial.append((address, signature, values))
            if signature == "isPool(address)":
                assert set(barrier.finished) == set(barrier.results)
                return (True,)
            if signature == "stable()": return (False,)
            if signature == "getFee(address,bool)":
                assert values == (POOL, False)
                return (30,)
            assert signature == "getAmountOut(uint256,address)" and values == (987, USDC)
            return (765,)
        engine.call = call
        row = await asyncio.wait_for(engine.quote({"pool": POOL, "dex": "aerodrome", "labels": []}, USDC, WETH, 987), 2)
        assert serial == [(AERO_FACTORY, "isPool(address)", (POOL,)), (POOL, "stable()", ()), (AERO_FACTORY, "getFee(address,bool)", (POOL, False)), (POOL, "getAmountOut(uint256,address)", (987, USDC))]
        assert row["amount_out_raw"] == "765" and row["fee_bps"] == 30 and row["stable"] is False
        assert row["gas_units_quote_estimate"] is None
    asyncio.run(run())


@pytest.mark.parametrize("operation,count", [("setup", 2), ("quote", 3)])
def test_caller_cancellation_drains_all_metadata_reads_before_return(operation, count):
    async def run():
        engine, started, stopped = pinned_engine(), [], []
        ready = asyncio.Event()
        async def rpc(method, params):
            return {"eth_chainId": "0x2105", "eth_blockNumber": BLOCK, "eth_getBlockByNumber": {"hash": HASH}}[method]
        async def call(address, signature, *args, **kwargs):
            started.append((address, signature))
            if len(started) == count: ready.set()
            try: await asyncio.Event().wait()
            finally: stopped.append((address, signature))
        engine.rpc, engine.call = rpc, call
        task = asyncio.create_task(engine.setup(USDC, WETH) if operation == "setup" else engine.quote(V3, USDC, WETH, 1))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert set(stopped) == set(started) and len(stopped) == count
            assert engine.pool_metadata == {}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_actual_rpc_reads_pin_block_and_preserve_revert_over_fee_decode_failure():
    async def run():
        requested, ready = [], asyncio.Event()
        token0 = keccak(text="token0()")[:4].hex()
        token1 = keccak(text="token1()")[:4].hex()
        async def respond(request):
            import json
            body = json.loads(request.content)
            assert body["method"] == "eth_call" and body["params"][1] == BLOCK
            transaction = body["params"][0]
            assert transaction["to"] == POOL and set(transaction) == {"to", "data"}
            requested.append(transaction["data"])
            if len(requested) == 3: ready.set()
            await ready.wait()
            selector = transaction["data"][2:10]
            if selector == token0: result = {"result": "0x" + encode(("address",), (USDC,)).hex()}
            elif selector == token1: result = {"error": {"code": 3, "message": "execution reverted"}}
            else: result = {"result": "0x"}  # Malformed fee must not mask token1's revert.
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, **result})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            engine = pinned_engine()
            engine.client, engine.rpc_urls = client, ["https://offline.invalid"]
            with pytest.raises(QuoteError) as caught:
                await asyncio.wait_for(engine.quote(V3, USDC, WETH, 1), 2)
            assert caught.value.code == "QUOTE_REVERTED" and len(requested) == 3
            assert engine.pool_metadata == {}
    asyncio.run(run())
