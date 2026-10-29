"""Bounded public evidence recovery; never uses payment endpoints or credentials."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from engine import base_dex, provider_reads as reads, threat_intel

TOKEN = base_dex.TOKENS["WETH"]


@pytest.fixture(autouse=True)
def clean_diagnostics(monkeypatch):
    monkeypatch.setattr(reads, "_DIAGNOSTICS", {})
    monkeypatch.setattr(reads, "_COOLDOWNS", {})
    monkeypatch.setattr(reads, "RETRY_DELAY_SECONDS", 0)


async def get_with(transport, provider="dexscreener"):
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        return await reads.read_public_json(client, provider, **({"token": TOKEN} if provider == "dexscreener" else {"addresses": [TOKEN]}))


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_transient_http_retries_once_then_returns_actual_json(status):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "0"}) if len(calls) == 1 else httpx.Response(200, json=[{"actual": True}])
    result = asyncio.run(get_with(respond))
    assert result.body == [{"actual": True}] and result.attempts == 2
    assert all(request.method == "GET" and request.url.host == "api.dexscreener.com" for request in calls)
    diagnostic = reads.provider_diagnostics()["dexscreener"]
    assert diagnostic["request_count"] == 2 and diagnostic["retry_count"] == 1
    assert diagnostic["last_failure_code"] == "DEXSCREENER_HTTP_" + str(status)
    assert diagnostic["last_failure_http_status"] == status


@pytest.mark.parametrize("status", [204, 301, 302, 400, 401, 403, 404, 422])
def test_permanent_status_and_redirect_are_not_retried_or_followed(status):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://untrusted.example/pay"}, text="private upstream message")
    with pytest.raises(reads.ProviderReadError) as error:
        asyncio.run(get_with(respond))
    assert len(calls) == 1 and error.value.code == "DEXSCREENER_HTTP_" + str(status)
    assert "private" not in str(error.value)


@pytest.mark.parametrize("kind", ["timeout", "network", "protocol", "json"])
def test_transient_transport_or_non_json_recovery_is_bounded_to_two_attempts(kind):
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 2: return httpx.Response(200, json={"actual": "evidence"})
        if kind == "timeout": raise httpx.ReadTimeout("secret timeout text", request=request)
        if kind == "network": raise httpx.ConnectError("secret network text", request=request)
        if kind == "protocol": raise httpx.RemoteProtocolError("secret protocol text", request=request)
        return httpx.Response(200, text="<html>temporary edge failure</html>")
    result = asyncio.run(get_with(respond, "goplus"))
    assert result.body == {"actual": "evidence"} and result.attempts == len(calls) == 2
    assert "secret" not in json.dumps(reads.provider_diagnostics())


def test_exhausted_retries_keep_original_error_code_and_actual_status():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "0"}, text="sensitive body")
    with pytest.raises(reads.ProviderReadError) as error:
        asyncio.run(get_with(respond))
    assert len(calls) == error.value.attempts == 2
    assert error.value.code == "DEXSCREENER_HTTP_503" and error.value.provider_http_status == 503


@pytest.mark.parametrize("value", ["120", format_datetime(datetime.now(timezone.utc) + timedelta(hours=1))])
def test_retry_after_that_exceeds_original_budget_is_honored_without_early_retry(value):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": value})
    with pytest.raises(reads.ProviderReadError) as error:
        asyncio.run(get_with(respond))
    assert len(calls) == 1 and error.value.code == "DEXSCREENER_HTTP_429"


def test_short_retry_after_is_used_inside_existing_budget(monkeypatch):
    sleeps, calls = [], []
    clock = [100.0]
    monkeypatch.setattr(reads, "_cooldown_now", lambda: clock[0])
    async def sleep(delay):
        sleeps.append(delay)
        clock[0] += delay
    monkeypatch.setattr(reads.asyncio, "sleep", sleep)
    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "0.2"}) if len(calls) == 1 else httpx.Response(200, json=[])
    asyncio.run(get_with(respond))
    assert sleeps == [0.2] and len(calls) == 2


def test_absolute_budget_cancels_inflight_http_without_fresh_deadline(monkeypatch):
    async def run():
        calls, cancelled = [], asyncio.Event()
        async def respond(request):
            calls.append(request)
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        with pytest.raises(reads.ProviderReadError) as error:
            await asyncio.wait_for(get_with(respond), 1.5)
        assert error.value.code == "DEXSCREENER_TIMEOUT" and len(calls) == 1
        assert cancelled.is_set() and reads.provider_diagnostics()["dexscreener"]["retry_count"] == 0
    monkeypatch.setattr(reads, "READ_BUDGET_SECONDS", 0.15)
    monkeypatch.setattr(reads, "ATTEMPT_TIMEOUT_SECONDS", 2)
    asyncio.run(run())


def test_caller_cancellation_never_triggers_retry_or_relabels_previous_evidence():
    async def run():
        calls, started, cancelled = [], asyncio.Event(), asyncio.Event()
        async def respond(request):
            calls.append(request)
            started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.set()
        task = asyncio.create_task(get_with(respond))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert cancelled.is_set() and len(calls) == 1
        assert reads.provider_diagnostics()["dexscreener"]["last_failure_code"] is None
    asyncio.run(run())


@pytest.mark.parametrize("args", [{"provider": "facilitator", "token": TOKEN}, {"provider": "dexscreener", "token": "https://evil.example"}, {"provider": "goplus", "addresses": [TOKEN] * 5}, {"provider": "goplus", "token": TOKEN}])
def test_helper_cannot_send_other_provider_payment_or_unbounded_requests(args):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected network request"))) as client:
            with pytest.raises(ValueError): await reads.read_public_json(client, **args)
    asyncio.run(run())


def test_large_json_response_is_rejected_without_retry(monkeypatch):
    monkeypatch.setattr(reads, "MAX_RESPONSE_BYTES", 10)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"large": "x" * 30})
    with pytest.raises(reads.ProviderReadError) as error: asyncio.run(get_with(respond))
    assert len(calls) == 1 and error.value.code == "DEXSCREENER_RESPONSE_TOO_LARGE"


@pytest.mark.parametrize("body,code,api_code", [({"code": 4012, "message": "sensitive vendor message"}, "GOPLUS_API_REJECTED", 4012), ({"code": True, "result": {}}, "GOPLUS_API_REJECTED", None), ({"code": 1, "result": {}}, "GOPLUS_NO_EVIDENCE", None), ({"code": 1, "result": []}, "GOPLUS_INVALID_SCHEMA", None), ([], "GOPLUS_INVALID_SCHEMA", None)])
def test_semantic_http200_failure_is_explicit_and_never_blindly_retried(body, code, api_code):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=body)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await threat_intel.fetch_threat_intel_state(client)
    with pytest.raises(reads.ProviderReadError) as error: asyncio.run(run())
    assert len(calls) == 1 and error.value.code == code and error.value.provider_api_code == api_code
    assert error.value.provider_http_status == 200
    assert reads.provider_diagnostics()["goplus"]["last_failure_api_code"] == api_code
    assert "sensitive" not in json.dumps(reads.provider_diagnostics())


def test_discovery_retries_preserve_oldest_original_retrieval_timestamp(monkeypatch):
    tokens = [(symbol, token) for symbol, token in base_dex.TOKENS.items() if symbol != "USDC"]
    stamps = {token: 1000 + index * 100 for index, (_symbol, token) in enumerate(tokens)}
    async def acquired(_client, provider, *, token):
        assert provider == "dexscreener"
        pool = {"chainId": "base", "pairAddress": "0x" + f"{list(stamps).index(token) + 1:040x}", "baseToken": {"address": token}, "quoteToken": {"address": base_dex.TOKENS["USDC"]}}
        return reads.ProviderJSON([pool], stamps[token], 2 if token == tokens[-1][1] else 1)
    monkeypatch.setattr(base_dex, "read_public_json", acquired)
    monkeypatch.setattr(base_dex.time, "time", lambda: 9999)
    result = asyncio.run(base_dex.market_snapshot(None))
    assert result["timestamp_ms"] == 1000 and len(result["pools"]) == 4 and result["errors"] == []


def test_discovery_failure_keeps_safe_per_token_causes_and_never_fabricates_pools():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(503, headers={"Retry-After": "0"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await base_dex.market_snapshot(client)
    result = asyncio.run(run())
    assert len(calls) == 8 and result["pools"] == [] and len(result["errors"]) == 4
    assert all(error["code"] == "DEXSCREENER_HTTP_503" and error["http_status"] == 503 for error in result["errors"])


@pytest.mark.parametrize("body,code", [([], "DEXSCREENER_NO_EVIDENCE"), ({}, "DEXSCREENER_INVALID_SCHEMA"), ([{"chainId": "base", "pairAddress": "0x" + "11" * 20, "baseToken": "malformed", "quoteToken": {}}], "DEXSCREENER_NO_EVIDENCE")])
def test_discovery_semantic_empty_or_unmatched_body_has_safe_cause_without_blind_retry(body, code):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=body)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await base_dex.market_snapshot(client)
    result = asyncio.run(run())
    assert len(calls) == 4 and result["pools"] == []
    assert len(result["errors"]) == 4 and all(error["code"] == code for error in result["errors"])
    assert reads.provider_diagnostics()["dexscreener"]["last_success_at_ms"] is None


def test_cancellation_during_backoff_does_not_count_a_request_never_sent(monkeypatch):
    async def run():
        sleeping = asyncio.Event()
        async def sleep(_delay):
            sleeping.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(reads.asyncio, "sleep", sleep)
        task = asyncio.create_task(get_with(lambda _request: httpx.Response(503)))
        await sleeping.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        state = reads.provider_diagnostics()["dexscreener"]
        assert state["request_count"] == 1 and state["retry_count"] == 0
    asyncio.run(run())


def test_diagnostic_code_and_api_fields_are_fixed_and_bounded():
    with pytest.raises(ValueError): reads.provider_error("goplus", "SENSITIVE_VENDOR_TEXT")
    for api_code in (True, 2**40, "sensitive"):
        error = reads.provider_error("goplus", "API_REJECTED", status=True, api_code=api_code)
        assert error.provider_http_status is None and error.provider_api_code is None
    diagnostic = reads.provider_diagnostics()
    assert set(diagnostic) == {"dexscreener", "goplus"}
    assert all("url" not in key and "body" not in key and "message" not in key for state in diagnostic.values() for key in state)


def test_diagnostic_projection_never_exposes_future_fields_and_latest_success_cannot_regress():
    reads.accept_provider_result("dexscreener", 2000)
    reads.accept_provider_result("dexscreener", 1000)
    reads._DIAGNOSTICS["dexscreener"]["future_raw_vendor_body"] = "sensitive provider content"
    value = reads.provider_diagnostics()
    assert value["dexscreener"]["last_success_at_ms"] == 2000
    assert "sensitive" not in json.dumps(value) and "future_raw_vendor_body" not in value["dexscreener"]
    assert set(value["dexscreener"]) == set(reads._METRIC_KEYS)
