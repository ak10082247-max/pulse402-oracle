"""Cross-cycle provider pauses retain actual evidence without extra HTTP calls."""
import asyncio
import copy
from datetime import datetime, timezone
from email.utils import format_datetime
import math

import httpx
import pytest

from engine import base_dex, provider_reads as reads

TOKEN = base_dex.TOKENS["WETH"]


@pytest.fixture(autouse=True)
def isolated_provider_state(monkeypatch):
    monkeypatch.setattr(reads, "_DIAGNOSTICS", {})
    monkeypatch.setattr(reads, "_COOLDOWNS", {})
    monkeypatch.setattr(reads, "RETRY_DELAY_SECONDS", 0)


async def dex_read(client, token=TOKEN):
    return await reads.read_public_json(client, "dexscreener", token=token)


@pytest.mark.parametrize("header", ["120", format_datetime(datetime.fromtimestamp(100120, timezone.utc))])
def test_cooldown_outlives_cycle_and_client_without_renewing_failure_or_counters(monkeypatch, header):
    clock = [100.0]
    monkeypatch.setattr(reads, "_cooldown_now", lambda: clock[0])
    monkeypatch.setattr(reads.time, "time", lambda: 100000.0)
    calls = []
    def blocked(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": header}, text="private upstream message")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(blocked)) as first:
            with pytest.raises(reads.ProviderReadError) as original:
                await dex_read(first)
        before = copy.deepcopy(reads.provider_diagnostics())
        deadline = reads._COOLDOWNS["dexscreener"]["until"]
        assert deadline == 220.0
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: pytest.fail("No HTTP during provider pause"))) as second:
            for token in (TOKEN, base_dex.TOKENS["AERO"], base_dex.TOKENS["DEGEN"]):
                clock[0] += 1
                with pytest.raises(reads.ProviderReadError) as cached:
                    await dex_read(second, token)
                assert cached.value is not original.value
                assert cached.value.code == "DEXSCREENER_HTTP_429"
                assert cached.value.provider_http_status == 429 and cached.value.attempts == 0
        assert len(calls) == 1 and reads.provider_diagnostics() == before
        assert reads._COOLDOWNS == {"dexscreener": {"until": deadline, "reason": "HTTP_429", "status": 429}}
    asyncio.run(run())


def test_other_fixed_provider_remains_independent_and_allowed_read_recovers_after_deadline(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reads, "_cooldown_now", lambda: clock[0])
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1: return httpx.Response(503, headers={"Retry-After": "120"})
        return httpx.Response(200, json={"actual": len(calls)})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(reads.ProviderReadError): await dex_read(client)
            failure_stamp = reads.provider_diagnostics()["dexscreener"]["last_failure_at_ms"]
            assert (await reads.read_public_json(client, "goplus", addresses=[TOKEN])).body == {"actual": 2}
            clock[0] = 220.001
            acquired = await dex_read(client)
            assert acquired.body == {"actual": 3} and acquired.attempts == 1
            reads.accept_provider_result("dexscreener", acquired.retrieved_at_ms)
            assert "dexscreener" not in reads._COOLDOWNS
            diagnostic = reads.provider_diagnostics()["dexscreener"]
            assert diagnostic["request_count"] == 2 and diagnostic["retry_count"] == 0
            assert diagnostic["last_failure_at_ms"] == failure_stamp
            assert diagnostic["last_success_at_ms"] == acquired.retrieved_at_ms
    asyncio.run(run())


def test_cancelled_backoff_neither_clears_nor_extends_pause_and_does_not_own_client(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reads, "_cooldown_now", lambda: clock[0])
    async def run():
        sleeping, calls = asyncio.Event(), []
        async def sleep(_delay):
            sleeping.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(reads.asyncio, "sleep", sleep)
        def respond(request):
            calls.append(request)
            return httpx.Response(429, headers={"Retry-After": "0.2"}) if len(calls) == 1 else httpx.Response(200, json=["fresh"])
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            task = asyncio.create_task(dex_read(client))
            await asyncio.wait_for(sleeping.wait(), 2)
            before = copy.deepcopy(reads.provider_diagnostics())
            cooldown = copy.deepcopy(reads._COOLDOWNS)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            clock[0] = 100.1
            with pytest.raises(reads.ProviderReadError): await dex_read(client)
            assert reads._COOLDOWNS == cooldown and reads.provider_diagnostics() == before
            assert not client.is_closed and len(calls) == 1
            clock[0] = 100.201
            assert (await dex_read(client)).body == ["fresh"]
            assert len(calls) == 2 and reads._COOLDOWNS == {}
    asyncio.run(run())


def test_only_an_actual_later_inflight_response_can_extend_existing_cooldown(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reads, "_cooldown_now", lambda: clock[0])
    async def run():
        calls, both_started, release_second = [], asyncio.Event(), asyncio.Event()
        async def respond(request):
            calls.append(request)
            index = len(calls)
            if index == 2: both_started.set()
            await both_started.wait()
            if index == 1: return httpx.Response(429, headers={"Retry-After": "120"})
            await release_second.wait()
            return httpx.Response(503, headers={"Retry-After": "180"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            first = asyncio.create_task(dex_read(client))
            second = asyncio.create_task(dex_read(client, base_dex.TOKENS["AERO"]))
            try:
                with pytest.raises(reads.ProviderReadError): await asyncio.wait_for(first, 2)
                assert reads._COOLDOWNS["dexscreener"]["until"] == 220
                clock[0] = 101
                release_second.set()
                with pytest.raises(reads.ProviderReadError): await asyncio.wait_for(second, 2)
                assert reads._COOLDOWNS["dexscreener"] == {"until": 281, "reason": "HTTP_503", "status": 503}
                before = copy.deepcopy(reads.provider_diagnostics())
                with pytest.raises(reads.ProviderReadError): await dex_read(client)
                assert len(calls) == 2 and reads.provider_diagnostics() == before
                assert reads._COOLDOWNS["dexscreener"]["until"] == 281
            finally:
                first.cancel(); second.cancel()
                await asyncio.gather(first, second, return_exceptions=True)
    asyncio.run(run())


def test_four_inflight_discovery_reads_keep_late_valid_pools_without_clearing_retry_after(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reads, "_cooldown_now", lambda: clock[0])
    async def run():
        calls, all_started, release_success = [], asyncio.Event(), asyncio.Event()
        async def respond(request):
            token = request.url.path.rsplit("/", 1)[1]
            calls.append(token)
            if len(calls) == 4: all_started.set()
            await all_started.wait()
            if token == TOKEN:
                return httpx.Response(429, headers={"Retry-After": "120"})
            await release_success.wait()
            return httpx.Response(200, json=[{
                "chainId": "base", "pairAddress": "0x" + f"{calls.index(token) + 1:040x}",
                "baseToken": {"address": token}, "quoteToken": {"address": base_dex.TOKENS["USDC"]},
            }])
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            task = asyncio.create_task(base_dex.market_snapshot(client))
            try:
                await asyncio.wait_for(all_started.wait(), 2)
                async def failure_recorded():
                    while "dexscreener" not in reads._COOLDOWNS:
                        await asyncio.sleep(0)
                await asyncio.wait_for(failure_recorded(), 2)
                cooldown = copy.deepcopy(reads._COOLDOWNS)
                before = copy.deepcopy(reads.provider_diagnostics()["dexscreener"])
                assert before["request_count"] == 4 and before["retry_count"] == 0
                assert not task.done()
                release_success.set()
                result = await asyncio.wait_for(task, 2)
                assert len(result["pools"]) == 3
                assert result["errors"] == [{"symbol": "WETH", "error": "DISCOVERY_UNAVAILABLE", "code": "DEXSCREENER_HTTP_429", "http_status": 429}]
                assert reads._COOLDOWNS == cooldown
                after = reads.provider_diagnostics()["dexscreener"]
                assert after["last_success_at_ms"] is not None
                assert after["last_failure_at_ms"] == before["last_failure_at_ms"]
                paused = await base_dex.market_snapshot(client)
                assert paused["pools"] == [] and len(paused["errors"]) == 4
                assert len(calls) == 4 and reads._COOLDOWNS == cooldown
                assert reads.provider_diagnostics()["dexscreener"] == after
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("value", ["NaN", "inf", "1e400", "invalid date", "invalid " * 81, "Mon, 01 Jan 1000 00:00:00 -0000"])
def test_invalid_nonfinite_or_unbounded_header_is_not_a_cooldown_or_retry_crash(value):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": value}) if len(calls) == 1 else httpx.Response(200, json=["actual"])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert (await dex_read(client)).body == ["actual"]
    asyncio.run(run())
    assert len(calls) == 2 and reads._COOLDOWNS == {}


def test_huge_finite_valid_delay_fails_immediately_without_truncating_or_sleeping(monkeypatch):
    monkeypatch.setattr(reads, "_cooldown_now", lambda: 100.0)
    calls = []
    async def forbidden_sleep(_delay): pytest.fail("Huge Retry-After must not sleep inside a bounded read")
    monkeypatch.setattr(reads.asyncio, "sleep", forbidden_sleep)
    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "9" * 81})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(reads.ProviderReadError): await dex_read(client)
            with pytest.raises(reads.ProviderReadError): await dex_read(client)
    asyncio.run(run())
    assert len(calls) == 1
    assert math.isfinite(reads._COOLDOWNS["dexscreener"]["until"])
    assert reads._COOLDOWNS["dexscreener"]["until"] == float("9" * 81)


def test_valid_decimal_beyond_float_capacity_fails_closed_without_unbounded_state(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reads, "_cooldown_now", lambda: clock[0])
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "9" * 4000})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(reads.ProviderReadError): await dex_read(client)
            before = copy.deepcopy(reads.provider_diagnostics())
            clock[0] = float.fromhex("0x1.fffffffffffffp+1023")
            with pytest.raises(reads.ProviderReadError): await dex_read(client)
            assert reads.provider_diagnostics() == before
    asyncio.run(run())
    cooldown = reads._COOLDOWNS["dexscreener"]
    assert len(calls) == 1 and len(reads._COOLDOWNS) == 1
    assert math.isfinite(cooldown["until"]) and cooldown["unrepresentable_delay"] is True


def test_valid_far_future_gmt_date_has_finite_pause_and_invalid_naive_date_never_crashes(monkeypatch):
    monkeypatch.setattr(reads.time, "time", lambda: 100000.0)
    delay = reads._retry_after(httpx.Response(429, headers={"Retry-After": "Fri, 31 Dec 9999 23:59:59 GMT"}))
    assert math.isfinite(delay) and delay > 1e11
    assert reads._retry_after(httpx.Response(429, headers={"Retry-After": "Mon, 01 Jan 1000 00:00:00 -0000"})) is None


def test_cooldown_keys_cannot_be_added_by_unknown_provider_or_untrusted_endpoint():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: pytest.fail("Invalid fixed provider"))) as client:
            for args in ({"provider": "attacker", "token": TOKEN}, {"provider": "dexscreener", "token": "https://attacker.invalid"}):
                with pytest.raises(ValueError): await reads.read_public_json(client, **args)
    asyncio.run(run())
    assert reads._COOLDOWNS == {} and reads.provider_diagnostics()["dexscreener"]["request_count"] == 0


def test_goplus_frequency_limit_4029_sets_cooldown_and_blocks_repeat_reads(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(reads, "_cooldown_now", lambda: clock[0])
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, json={"code": 4029, "message": "Frequency limit exceeded"})
        return httpx.Response(200, json={"code": 1, "result": {base_dex.TOKENS["WETH"]: {"token_symbol": "WETH", "is_honeypot": "0"}}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            from engine import threat_intel
            with pytest.raises(reads.ProviderReadError) as err:
                await threat_intel.fetch_threat_intel_state(client)
            assert err.value.code == "GOPLUS_API_REJECTED"
            assert err.value.provider_api_code == 4029
            assert err.value.provider_http_status == 200
            assert reads._COOLDOWNS.get("goplus") is not None
            assert reads._COOLDOWNS["goplus"]["reason"] == "API_REJECTED"
            assert reads._COOLDOWNS["goplus"]["api_code"] == 4029
            assert reads._COOLDOWNS["goplus"]["status"] == 200
            assert reads._COOLDOWNS["goplus"]["until"] == 115.0

            # During cooldown, read_public_json raises without HTTP call
            with pytest.raises(reads.ProviderReadError) as cached:
                await reads.read_public_json(client, "goplus", addresses=[base_dex.TOKENS["WETH"]])
            assert cached.value.code == "GOPLUS_API_REJECTED"
            assert cached.value.provider_api_code == 4029
            assert cached.value.attempts == 0
            assert len(calls) == 1

            # After cooldown expires, read succeeds
            clock[0] = 115.001
            res = await threat_intel.fetch_threat_intel_state(client)
            assert len(calls) == 2
            assert res["timestamp_ms"] is not None
    asyncio.run(run())
