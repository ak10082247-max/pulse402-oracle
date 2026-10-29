"""Unsigned aggregation, captured on-chain parity and adversarial RPC regressions."""
import asyncio
import copy
import json
import time
from pathlib import Path

import httpx
import pytest
from eth_abi import decode, encode
from eth_utils import keccak

from engine import base_dex, multicall_reads as multi
from engine.base_quotes import UNI_FACTORY, UNI_QUOTER, QuoteError

FIXTURE = json.loads((Path(__file__).parent / "fixtures/base-multicall-parity-20261029.json").read_text(encoding="utf-8"))
CODE = FIXTURE["runtime_code"]
BLOCK = "0x64"
HASH = "0x" + "ab" * 32
TOKENS = set(base_dex.TOKENS.values())
USDC, WETH = base_dex.TOKENS["USDC"], base_dex.TOKENS["WETH"]
POOL = "0x" + "12" * 20
UNKNOWN = "0x" + "34" * 20


def row(index=1, address=USDC, signature="decimals()", types=(), values=()):
    return {"jsonrpc": "2.0", "id": index, "method": "eth_call", "params": [{"to": address, "data": "0x" + (keccak(text=signature)[:4] + encode(types, values)).hex()}, BLOCK]}


def wrapped(values):
    return {"jsonrpc": "2.0", "id": 1, "result": "0x" + encode(("(bool,bytes)[]",), (values,)).hex()}


def test_actual_captured_runtime_and_direct_quote_parity_including_repeated_quote():
    assert FIXTURE["chain_id"] == 8453 and multi.code_matches(CODE)
    assert not multi.code_matches(CODE[:-2] + "00") and not multi.code_matches("0x")
    calls = FIXTURE["calls"]
    direct = {item["id"]: item for item in FIXTURE["direct_responses"]}
    aggregate = multi.results(FIXTURE["aggregate_response"], calls)
    assert all(multi.reviewed(item["params"], FIXTURE["block"]["number"], TOKENS, set()) for item in calls[:6])
    assert not multi.reviewed(calls[-1]["params"], FIXTURE["block"]["number"], TOKENS, set())
    for combined in aggregate:
        single = direct[combined["id"]]
        if combined["id"] in (4, 5, 6):
            types = ("uint256", "uint160", "uint32", "uint256")
            assert decode(types, bytes.fromhex(single["result"][2:]))[:3] == decode(types, bytes.fromhex(combined["result"][2:]))[:3]
        elif combined["id"] == 7:
            assert single.get("error") and combined.get("error")
        else:
            assert single["result"] == combined["result"]
    # A different-sized quote between the identical quotes does not mutate the
    # next quote's amount/price/ticks. Warming changes only the gas diagnostic.
    assert aggregate[3]["result"][:194] == aggregate[5]["result"][:194]


@pytest.mark.parametrize("item,pools", [
    (row(), set()),
    (row(address=WETH, signature="balanceOf(address)", types=("address",), values=(UNKNOWN,)), set()),
    (row(address=UNI_FACTORY, signature="getPool(address,address,uint24)", types=("address", "address", "uint24"), values=(USDC, WETH, 500)), set()),
    (row(address=UNI_QUOTER, signature="quoteExactInputSingle((address,address,uint256,uint24,uint160))", types=("(address,address,uint256,uint24,uint160)",), values=((USDC, WETH, 1, 500, 0),)), set()),
    (row(address=POOL, signature="token0()"), {POOL}),
    (row(address=POOL, signature="token1()"), {POOL}),
    (row(address=POOL, signature="fee()"), {POOL}),
])
def test_only_reviewed_caller_independent_selectors_and_targets_are_eligible(item, pools):
    assert multi.reviewed(item["params"], BLOCK, TOKENS, pools)


@pytest.mark.parametrize("change", ["unknown_target", "unknown_selector", "pool_unverified", "wrong_block", "latest", "from", "value", "gas", "extra_params", "trailing_bytes", "truncated_bytes", "odd_hex", "invalid_address"])
def test_unsupported_or_context_dependent_reads_remain_isolated(change):
    item = row()
    transaction = item["params"][0]
    if change == "unknown_target": transaction["to"] = UNKNOWN
    elif change == "unknown_selector": transaction["data"] = "0xffffffff"
    elif change == "pool_unverified": item = row(address=POOL, signature="token0()")
    elif change == "wrong_block": item["params"][1] = "0x65"
    elif change == "latest": item["params"][1] = "latest"
    elif change in ("from", "value", "gas"): transaction[change] = "0x0"
    elif change == "extra_params": item["params"].append({})
    elif change == "trailing_bytes": transaction["data"] += "00" * 32
    elif change == "truncated_bytes": transaction["data"] = "0x12"
    elif change == "odd_hex": transaction["data"] += "0"
    elif change == "invalid_address": transaction["to"] = "invalid"
    assert not multi.reviewed(item["params"], BLOCK, TOKENS, set())


@pytest.mark.parametrize("first,second,amount,fee,limit", [(UNKNOWN, WETH, 1, 500, 0), (USDC, USDC, 1, 500, 0), (USDC, WETH, 0, 500, 0), (USDC, WETH, 1, 777, 0), (USDC, WETH, 1, 500, 1)])
def test_quoter_rejects_unknown_token_pair_fee_zero_amount_and_custom_price_limit(first, second, amount, fee, limit):
    item = row(address=UNI_QUOTER, signature="quoteExactInputSingle((address,address,uint256,uint24,uint160))", types=("(address,address,uint256,uint24,uint160)",), values=((first, second, amount, fee, limit),))
    assert not multi.reviewed(item["params"], BLOCK, TOKENS, set())


def test_request_is_exact_bounded_same_block_no_value_allows_individual_reverts():
    items = [row(9), row(2, WETH)]
    payload = multi.request(items, BLOCK)
    outer, block = payload["params"]
    assert block == BLOCK and outer["to"] == multi.ADDRESS_MULTICALL
    assert set(outer) == {"to", "data", "gas"} and int(outer["gas"], 16) == multi.GAS_CEILING
    raw = bytes.fromhex(outer["data"][2:])
    assert raw[:4] == multi.SELECTOR
    calls = decode(("(address,bool,bytes)[]",), raw[4:])[0]
    assert [(target, allowed, data.hex()) for target, allowed, data in calls] == [(item["params"][0]["to"].lower(), True, item["params"][0]["data"][2:]) for item in items]
    for invalid in ([], [row()] * 41, [{**row(), "method": "eth_sendRawTransaction"}]):
        with pytest.raises(ValueError): multi.request(invalid, BLOCK)


def test_each_result_retains_original_id_and_per_call_failure_without_losing_success():
    items = [row(17), row(3), row(101)]
    result = multi.results(wrapped([(True, b"\x01"), (False, b"revert reason"), (True, b"\x03")]), items)
    assert result[0] == {"jsonrpc": "2.0", "id": 17, "result": "0x01"}
    assert result[1]["id"] == 3 and result[1]["error"]["code"] == 3
    assert result[2] == {"jsonrpc": "2.0", "id": 101, "result": "0x03"}


@pytest.mark.parametrize("change", ["wrong_id", "bool_id", "wrong_version", "with_error", "wrong_count", "truncated", "trailing", "huge_outer", "huge_inner", "not_hex", "not_dict"])
def test_malformed_oversized_or_mismatched_aggregate_response_is_refused(change):
    value = wrapped([(True, b"ok")])
    if change == "wrong_id": value["id"] = 2
    elif change == "bool_id": value["id"] = True
    elif change == "wrong_version": value["jsonrpc"] = "1.0"
    elif change == "with_error": value["error"] = {"code": 3}
    elif change == "wrong_count": value = wrapped([])
    elif change == "truncated": value["result"] = value["result"][:-2]
    elif change == "trailing": value["result"] += "00" * 32
    elif change == "huge_outer": value["result"] = "0x" + "00" * 262145
    elif change == "huge_inner": value = wrapped([(True, b"x" * 8193)])
    elif change == "not_hex": value["result"] = "0xzz"
    elif change == "not_dict": value = []
    with pytest.raises(QuoteError): multi.results(value, [row()])


class Provider:
    def __init__(self, primary="ok", fallback="ok", inner_revert=False, isolated_bad=False, aggregate_bad=False, block_envelope_bad=False, empty_failure=False, code_envelope_bad=None, chain_envelope_bad=None):
        self.primary, self.fallback = primary, fallback
        self.inner_revert, self.isolated_bad = inner_revert, isolated_bad
        self.aggregate_bad, self.block_envelope_bad = aggregate_bad, block_envelope_bad
        self.empty_failure, self.code_envelope_bad, self.chain_envelope_bad = empty_failure, code_envelope_bad, chain_envelope_bad
        self.calls = []

    def respond(self, request):
        body = json.loads(request.content)
        host = request.url.host
        self.calls.append((host, body))
        condition = self.primary if host == "base-rpc.publicnode.com" else self.fallback
        if isinstance(body, list):
            value = {"error": "bad batch"} if self.isolated_bad else [{"jsonrpc": "2.0", "id": item["id"], "result": "0xbeef"} for item in reversed(body)]
        elif body["method"] == "eth_chainId": value = "0x1" if condition == "wrong_chain" else "0x2105"
        elif body["method"] == "eth_getBlockByNumber":
            assert body["params"] == [BLOCK, False]
            value = {"hash": "0x" + "cd" * 32 if condition == "wrong_block" else HASH}
            if self.block_envelope_bad:
                return httpx.Response(200, json={"jsonrpc": "1.0", "id": 999, "result": value, "error": {"code": -32000}})
        elif body["method"] == "eth_getCode":
            assert body["params"] == [multi.ADDRESS_MULTICALL, BLOCK]
            value = "0x00" if condition == "bad_code" else CODE
        else:
            assert body["method"] == "eth_call"
            assert body["params"][1] == BLOCK
            assert body["params"][0]["to"] == multi.ADDRESS_MULTICALL
            if condition == "rate_limit": return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "rate limit exceeded"}})
            raw = bytes.fromhex(body["params"][0]["data"][2:])
            calls = decode(("(address,bool,bytes)[]",), raw[4:])[0]
            value = "0x00" if self.aggregate_bad else wrapped([(not (self.inner_revert and index == 1), b"" if self.empty_failure and index == 1 else encode(("uint256",), (index + 6,))) for index, _ in enumerate(calls)])["result"]
        if not isinstance(body, list):
            malformed = self.code_envelope_bad if body["method"] == "eth_getCode" else self.chain_envelope_bad if body["method"] == "eth_chainId" else None
            if malformed:
                envelope = {"jsonrpc": "2.0", "id": True if malformed == "bool_id" else 1, "result": value}
                if malformed == "error_null": envelope["error"] = None
                return httpx.Response(200, json=envelope)
        return httpx.Response(200, json=value if isinstance(body, list) else {"jsonrpc": "2.0", "id": 1, "result": value})

    def requests(self, method, host=None):
        return [payload for domain, payload in self.calls if isinstance(payload, dict) and payload.get("method") == method and (host is None or domain == host)]


async def acquire(provider, items):
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider.respond)) as client:
        engine = base_dex.CycleQuotes(client, {"pools": []})
        engine.block, engine.block_info = BLOCK, {"hash": HASH}
        values = await asyncio.gather(*(engine.rpc("eth_call", item["params"]) for item in items), return_exceptions=True)
        await engine.close_pending()
        return values, engine


def test_81_reviewed_reads_use_three_bounded_rpc_items_and_verify_code_once():
    provider = Provider()
    values, engine = asyncio.run(acquire(provider, [row(index) for index in range(81)]))
    assert all(isinstance(value, str) for value in values)
    assert len(provider.requests("eth_call")) == 3 and len(provider.requests("eth_getCode")) == 1
    assert len(provider.requests("eth_chainId")) == 1 and len(provider.requests("eth_getBlockByNumber")) == 1
    assert engine.read_counts == {"aggregated_inner_reads": 81, "aggregate_rpc_items": 3, "isolated_read_items": 0, "aggregate_fallbacks": 0, "aggregate_budget_unknown_reads": 0}


@pytest.mark.parametrize("isolated_bad", [False, True])
def test_unknown_reads_never_aggregate_and_malformed_isolated_group_preserves_approved(isolated_bad):
    provider = Provider(isolated_bad=isolated_bad)
    unknown = row(3, UNKNOWN)
    values, engine = asyncio.run(acquire(provider, [row(1), unknown]))
    assert values[0] == "0x" + encode(("uint256",), (6,)).hex()
    assert isinstance(values[1], QuoteError) if isolated_bad else values[1] == "0xbeef"
    batches = [payload for _, payload in provider.calls if isinstance(payload, list)]
    assert len(batches) == 1 and batches[0][0]["params"] == unknown["params"]
    aggregate = provider.requests("eth_call")[0]
    inner = decode(("(address,bool,bytes)[]",), bytes.fromhex(aggregate["params"][0]["data"][10:]))[0]
    assert len(inner) == 1 and inner[0][0] == USDC


def test_inner_revert_is_reported_once_without_retrying_or_poisoning_other_results():
    provider = Provider(inner_revert=True)
    values, _ = asyncio.run(acquire(provider, [row(1), row(2), row(3)]))
    assert isinstance(values[0], str) and isinstance(values[1], QuoteError) and isinstance(values[2], str)
    assert len(provider.requests("eth_call")) == 1
    assert not any(host == "mainnet.base.org" for host, _ in provider.calls)


@pytest.mark.parametrize("primary,fallback,works", [("bad_code", "ok", True), ("bad_code", "bad_code", True), ("rate_limit", "ok", True), ("rate_limit", "bad_code", True), ("rate_limit", "wrong_chain", True), ("rate_limit", "wrong_block", True), ("wrong_chain", "ok", True), ("wrong_block", "ok", True), ("wrong_chain", "wrong_chain", False), ("wrong_block", "wrong_block", False)])
def test_every_fallback_requires_same_chain_pinned_hash_and_canonical_multicall_code(primary, fallback, works):
    provider = Provider(primary, fallback)
    values, _ = asyncio.run(acquire(provider, [row()]))
    assert isinstance(values[0], str) if works else isinstance(values[0], QuoteError)
    if primary in ("bad_code", "wrong_chain", "wrong_block"):
        assert not provider.requests("eth_call", "base-rpc.publicnode.com")
    if fallback in ("bad_code", "wrong_chain", "wrong_block"):
        assert not provider.requests("eth_call", "mainnet.base.org")
    if works and fallback == "ok":
        assert len(provider.requests("eth_getCode", "mainnet.base.org")) == 1


@pytest.mark.parametrize("bad_code", [False, True])
def test_whole_aggregate_failure_uses_one_bounded_original_batch_without_invalid_code(bad_code):
    provider = Provider("bad_code" if bad_code else "ok", "bad_code" if bad_code else "ok", aggregate_bad=not bad_code)
    values, engine = asyncio.run(acquire(provider, [row(1), row(2)]))
    assert values == ["0xbeef", "0xbeef"]
    assert engine.read_counts == {"aggregated_inner_reads": 2, "aggregate_rpc_items": 1, "isolated_read_items": 2, "aggregate_fallbacks": 1, "aggregate_budget_unknown_reads": 0}
    assert len(provider.requests("eth_call")) == (0 if bad_code else 1)
    assert len([payload for _, payload in provider.calls if isinstance(payload, list)]) == 1


def test_provider_block_hash_alone_does_not_override_invalid_response_envelope():
    provider = Provider(block_envelope_bad=True)
    values, _ = asyncio.run(acquire(provider, [row()]))
    assert isinstance(values[0], QuoteError)
    assert not provider.requests("eth_getCode") and not provider.requests("eth_call")
    assert not any(isinstance(payload, list) for _, payload in provider.calls)


@pytest.mark.parametrize("kind", ["bool_id", "error_null"])
@pytest.mark.parametrize("method", ["code", "chain"])
def test_code_and_chain_envelopes_reject_boolean_ids_or_error_even_with_valid_result(kind, method):
    provider = Provider(**{method + "_envelope_bad": kind})
    values, _ = asyncio.run(acquire(provider, [row()]))
    assert not provider.requests("eth_call")
    if method == "chain":
        assert isinstance(values[0], QuoteError) and not any(isinstance(payload, list) for _, payload in provider.calls)
    else:
        assert values == ["0xbeef"]  # Original isolated reads need no Multicall code.


def test_expensive_quoter_groups_have_at_most_four_simulations_with_all_sizes_preserved():
    provider = Provider()
    items = [row(index, UNI_QUOTER, "quoteExactInputSingle((address,address,uint256,uint24,uint160))", ("(address,address,uint256,uint24,uint160)",), ((USDC, WETH, index + 1, 500, 0),)) for index in range(9)]
    values, engine = asyncio.run(acquire(provider, items))
    assert all(isinstance(value, str) for value in values)
    requests = provider.requests("eth_call")
    decoded = [decode(("(address,bool,bytes)[]",), bytes.fromhex(request["params"][0]["data"][10:]))[0] for request in requests]
    assert [len(calls) for calls in decoded] == [4, 4, 1]
    assert [data.hex() for calls in decoded for _, _, data in calls] == [item["params"][0]["data"][2:] for item in items]
    assert engine.read_counts["aggregate_fallbacks"] == 0


def test_empty_inner_failure_reacquires_only_uncertain_read_once_not_success_or_reasoned_revert():
    provider = Provider(inner_revert=True, empty_failure=True)
    values, engine = asyncio.run(acquire(provider, [row(1), row(2), row(3)]))
    assert isinstance(values[0], str) and values[1] == "0xbeef" and isinstance(values[2], str)
    assert len(provider.requests("eth_call")) == 1
    batches = [payload for _, payload in provider.calls if isinstance(payload, list)]
    assert len(batches) == 1 and [item["id"] for item in batches[0]] == [2]
    assert engine.read_counts["aggregate_budget_unknown_reads"] == 1
    assert engine.read_counts["isolated_read_items"] == 1 and engine.read_counts["aggregate_fallbacks"] == 0


@pytest.mark.parametrize("failure", ["malformed", "deadline"])
def test_uncertain_sibling_retry_failure_does_not_erase_success_or_reasoned_revert(monkeypatch, failure):
    provider = Provider()
    async def respond(request):
        body = json.loads(request.content)
        if isinstance(body, list):
            provider.calls.append((request.url.host, body))
            assert len(body) == 1 and body[0]["id"] == 2
            if failure == "deadline": await asyncio.Event().wait()
            return httpx.Response(200, json={"error": "not a batch"})
        if body["method"] == "eth_call":
            provider.calls.append((request.url.host, body))
            return httpx.Response(200, json=wrapped([(True, b"good"), (False, b""), (False, b"reasoned revert")]))
        return provider.respond(request)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            engine = base_dex.CycleQuotes(client, {"pools": []})
            engine.block, engine.block_info = BLOCK, {"hash": HASH}
            engine.observed_at_ms = int(time.time() * 1000)
            values = await asyncio.gather(*(engine.rpc("eth_call", row(index)["params"]) for index in range(3)), return_exceptions=True)
            await engine.close_pending()
            return values, engine
    monkeypatch.setattr(base_dex, "MAX_QUOTE_AGE_MS", 50)
    values, engine = asyncio.run(run())
    assert values[0] == "0x" + b"good".hex()
    assert isinstance(values[1], QuoteError) and isinstance(values[2], QuoteError)
    assert len(provider.requests("eth_call")) == 1
    assert engine.read_counts["aggregate_budget_unknown_reads"] == 1
    assert len([body for _, body in provider.calls if isinstance(body, list)]) == 1


def test_expired_original_quote_does_not_gain_an_aggregate_or_fallback_deadline():
    async def run():
        provider = Provider()
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider.respond)) as client:
            engine = base_dex.CycleQuotes(client, {"pools": []})
            engine.block, engine.block_info, engine.observed_at_ms = BLOCK, {"hash": HASH}, 1
            values = await asyncio.gather(engine.rpc("eth_call", row()["params"]), return_exceptions=True)
            await engine.close_pending()
            return values, provider
    values, provider = asyncio.run(run())
    assert isinstance(values[0], QuoteError) and not provider.calls


@pytest.mark.parametrize("malformed", ["bool_id", "error_null"])
def test_non_call_rpc_envelope_also_rejects_boolean_id_or_error_presence(malformed):
    def response(request):
        payload = {"jsonrpc": "2.0", "id": True if malformed == "bool_id" else 1, "result": BLOCK}
        if malformed == "error_null": payload["error"] = None
        return httpx.Response(200, json=payload)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            engine = base_dex.CycleQuotes(client, {"pools": []})
            engine.selected_provider = "https://base-rpc.publicnode.com"
            return await asyncio.gather(engine.rpc("eth_blockNumber", []), return_exceptions=True)
    assert isinstance(asyncio.run(run())[0], QuoteError)


@pytest.mark.parametrize("failure", ["timeout", "non_json"])
@pytest.mark.parametrize("both_providers", [False, True])
def test_parallel_groups_cache_failed_code_acquisition_once_per_provider_and_cycle(failure, both_providers):
    class BrokenCode(Provider):
        async def respond(self, request):
            body = json.loads(request.content)
            if isinstance(body, dict) and body["method"] == "eth_getCode" and (both_providers or request.url.host == "base-rpc.publicnode.com"):
                self.calls.append((request.url.host, body))
                await asyncio.sleep(0)  # Allow sibling groups to queue on verification.
                if failure == "timeout": raise httpx.ReadTimeout("unavailable public code read", request=request)
                return httpx.Response(200, text="not JSON")
            return super().respond(request)
    provider = BrokenCode()
    items = [row(index, UNI_QUOTER, "quoteExactInputSingle((address,address,uint256,uint24,uint160))", ("(address,address,uint256,uint24,uint160)",), ((USDC, WETH, index + 1, 500, 0),)) for index in range(9)]
    values, engine = asyncio.run(acquire(provider, items))
    assert all(isinstance(value, str) for value in values)
    assert len(provider.requests("eth_getCode", "base-rpc.publicnode.com")) == 1
    assert len(provider.requests("eth_getCode", "mainnet.base.org")) == 1
    assert engine.read_counts["aggregate_fallbacks"] == (3 if both_providers else 0)


def test_gas_diagnostic_discloses_context_instead_of_claiming_transaction_cost(monkeypatch):
    async def quoted(*args, **kwargs): return {"gas_units_quote_estimate": 123}
    monkeypatch.setattr(base_dex.BaseQuotes, "quote", quoted)
    value = asyncio.run(base_dex.CycleQuotes(None, {"pools": []}).quote())
    assert "Context-dependent" in value["gas_estimate_semantics"] and "Not transaction gas" in value["gas_estimate_semantics"]
