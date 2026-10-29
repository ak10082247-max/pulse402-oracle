"""Safe operational failure evidence without provider or paid record disclosure."""
import asyncio
import copy
import json
import logging

import httpx
import pytest

from engine import poller
from engine.base_quotes import QuoteError

SECRET = "PRIVATE_PROVIDER_TOKEN_AND_PAID_RECORD"
CALLBACKS = (
    "fetch_base_liquidity_state", "fetch_polymarket_velocity_state",
    "fetch_threat_intel_state", "fetch_arb_routes_state",
)


@pytest.fixture(autouse=True)
def isolated_feed_state(monkeypatch):
    monkeypatch.setattr(poller, "_FEED_SNAPSHOTS", {})
    monkeypatch.setattr(poller, "_FEED_EVENTS", {})
    monkeypatch.setattr(poller, "_ERRORS", {})
    monkeypatch.setattr(poller, "_ACQUISITION_METRICS", {})
    monkeypatch.setattr(poller, "STATE", copy.deepcopy(poller.STATE))


def http_error(status):
    request = httpx.Request("GET", f"https://offline.invalid/{SECRET}", headers={"Authorization": SECRET})
    response = httpx.Response(status, request=request, text=SECRET)
    return httpx.HTTPStatusError(SECRET, request=request, response=response)


@pytest.mark.parametrize("error,expected,status", [
    (http_error(429), "HTTP_429", 429), (http_error(503), "HTTP_503", 503),
    (QuoteError("STALE_QUOTE", SECRET), "STALE_QUOTE", None),
    (QuoteError("https://offline.invalid/" + SECRET, SECRET), "QuoteError", None),
    (QuoteError("X" * 65, SECRET), "QuoteError", None),
    (TimeoutError(SECRET), "TimeoutError", None),
    (httpx.ReadTimeout(SECRET), "ReadTimeout", None),
    (ValueError(SECRET), "ValueError", None),
])
def test_error_details_are_bounded_codes_and_actual_provider_http_status_only(error, expected, status):
    assert poller._safe_error_details(error) == (expected, status)
    assert SECRET not in json.dumps(poller._safe_error_details(error))


def test_typed_provider_error_uses_safe_code_and_actual_status_not_service_status():
    error = QuoteError("GOPLUS_HTTP_5XX", SECRET, 503)
    assert poller._safe_error_details(error) == ("GOPLUS_HTTP_5XX", None)
    error.provider_http_status = 502
    assert poller._safe_error_details(error) == ("GOPLUS_HTTP_5XX", 502)
    error.provider_http_status = True
    assert poller._safe_error_details(error) == ("GOPLUS_HTTP_5XX", None)


def test_actual_per_feed_duration_publishes_before_slow_siblings_complete(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(poller.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(poller.time, "time", lambda: 100.0)

    async def run():
        release = asyncio.Event()
        async def fast(_client):
            clock[0] = 10.125
            return [{"timestamp_ms": 99000, "paid": SECRET}]
        async def slow(_client):
            await release.wait()
            return [{"timestamp_ms": 99500}]
        monkeypatch.setattr(poller, CALLBACKS[0], fast)
        for callback in CALLBACKS[1:]: monkeypatch.setattr(poller, callback, slow)
        task = asyncio.create_task(poller.run_single_poll_cycle(None))
        try:
            async def first_completed():
                while "base-liquidity" not in poller._ACQUISITION_METRICS:
                    await asyncio.sleep(0)
            await asyncio.wait_for(first_completed(), 2)
            assert not task.done()
            assert poller.get_feed_diagnostics("base-liquidity") == {
                "last_acquisition_duration_ms": 125, "last_acquisition_completed_at_ms": 100000,
                "last_acquisition_error_code": None, "last_acquisition_http_status": None,
                "last_acquisition_api_code": None,
            }
            assert poller.get_feed_diagnostics("threat-intel")["last_acquisition_duration_ms"] is None
            clock[0] = 10.5
            release.set()
            await task
            assert poller.get_feed_diagnostics("threat-intel")["last_acquisition_duration_ms"] == 500
            assert poller.get_feed_state("base-liquidity")["timestamp_ms"] == 99000
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_failure_retains_expired_observation_and_logs_only_safe_structured_fields(monkeypatch, caplog):
    monkeypatch.setattr(poller.time, "time", lambda: 100.0)
    poller._publish("base-liquidity", [{"timestamp_ms": 80000, "paid": SECRET}])
    before = copy.deepcopy(poller._FEED_SNAPSHOTS["base-liquidity"])
    async def failed(_client): raise http_error(429)
    async def good(_client): return [{"timestamp_ms": 100000}]
    monkeypatch.setattr(poller, CALLBACKS[0], failed)
    for callback in CALLBACKS[1:]: monkeypatch.setattr(poller, callback, good)
    with caplog.at_level(logging.DEBUG, logger="pulse402.poller"):
        asyncio.run(poller.run_single_poll_cycle(None))
    assert poller._FEED_SNAPSHOTS["base-liquidity"] == before
    assert poller.get_feed_state("base-liquidity")["status"] == "stale"
    assert poller._ERRORS == {"base-liquidity": "HTTP_429"}
    diagnostics = poller.get_feed_diagnostics("base-liquidity")
    assert diagnostics["last_acquisition_error_code"] == "HTTP_429"
    assert diagnostics["last_acquisition_http_status"] == 429
    assert diagnostics["last_acquisition_duration_ms"] >= 0
    assert SECRET not in caplog.text and "offline.invalid" not in caplog.text
    failures = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(failures) == 1
    assert set(json.loads(failures[0].message.removeprefix("feed_acquisition "))) == {"feed", "duration_ms", "completed_at_ms", "error_code", "http_status", "api_code"}


def test_successful_recovery_clears_current_error_and_records_recovery_without_retimestamps(monkeypatch, caplog):
    monkeypatch.setattr(poller.time, "time", lambda: 100.0)
    poller._ERRORS["threat-intel"] = "ReadTimeout"
    async def good(_client): return [{"timestamp_ms": 99000, "paid": SECRET}]
    for callback in CALLBACKS: monkeypatch.setattr(poller, callback, good)
    with caplog.at_level(logging.INFO, logger="pulse402.poller"):
        asyncio.run(poller.run_single_poll_cycle(None))
    assert poller._ERRORS == {}
    assert poller.get_feed_diagnostics("threat-intel")["last_acquisition_error_code"] is None
    assert poller.get_feed_state("threat-intel")["timestamp_ms"] == 99000
    assert SECRET not in caplog.text
    assert len(caplog.records) == 1 and '"feed": "threat-intel"' in caplog.text


def test_shutdown_cancellation_preserves_last_completed_metrics_and_drains_callbacks(monkeypatch):
    previous = {"duration_ms": 1234, "completed_at_ms": 90000, "error_code": "TimeoutError", "http_status": None}
    monkeypatch.setattr(poller, "_ACQUISITION_METRICS", {feed: copy.deepcopy(previous) for feed in poller.FEEDS})
    async def run():
        started, stopped = [], []
        ready = asyncio.Event()
        async def blocked(_client):
            started.append(True)
            if len(started) == len(poller.FEEDS): ready.set()
            try: await asyncio.Event().wait()
            finally: stopped.append(True)
        for callback in CALLBACKS: monkeypatch.setattr(poller, callback, blocked)
        task = asyncio.create_task(poller.run_single_poll_cycle(None))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert len(stopped) == len(poller.FEEDS)
            assert poller._ACQUISITION_METRICS == {feed: previous for feed in poller.FEEDS}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_health_and_preview_keep_paid_data_out_of_new_diagnostics(monkeypatch):
    import app
    monkeypatch.setattr(poller.time, "time", lambda: 100.0)
    for feed in poller.FEEDS:
        poller._publish(feed, [{"timestamp_ms": 99000, "paid": SECRET}])
        poller._ACQUISITION_METRICS[feed] = {"duration_ms": 875, "completed_at_ms": 100000, "error_code": "STALE_QUOTE", "http_status": None, "paid": SECRET}
    health = asyncio.run(app.health_check())
    assert SECRET not in json.dumps(health)
    assert health["feed_status"]["base-liquidity"]["last_acquisition_duration_ms"] == 875
    assert health["feed_status"]["base-liquidity"]["last_acquisition_error_code"] == "STALE_QUOTE"
    assert health["feed_status"]["base-liquidity"]["observed_at_ms"] == 99000
    preview = app.preview_payload("base-liquidity")
    assert SECRET not in json.dumps(preview) and "last_acquisition_duration_ms" not in json.dumps(preview)
    assert poller.get_feed_state("base-liquidity")["data"][0]["paid"] == SECRET


def test_uninitialized_diagnostics_are_explicit_and_unknown_feed_is_rejected():
    assert poller.get_feed_diagnostics("base-liquidity") == {
        "last_acquisition_duration_ms": None, "last_acquisition_completed_at_ms": None,
        "last_acquisition_error_code": None, "last_acquisition_http_status": None,
        "last_acquisition_api_code": None,
    }
    with pytest.raises(ValueError): poller.get_feed_diagnostics("unknown")


@pytest.mark.parametrize("api_code,expected", [(4029, 4029), (True, None), ("429", None), (2**31, None)])
def test_semantic_provider_failure_preserves_only_bounded_numeric_api_code(monkeypatch, caplog, api_code, expected):
    error = QuoteError("GOPLUS_API_REJECTED", SECRET)
    error.provider_http_status, error.provider_api_code = 200, api_code
    async def failed(_client): raise error
    async def good(_client): return [{"timestamp_ms": int(poller.time.time() * 1000)}]
    monkeypatch.setattr(poller, CALLBACKS[0], failed)
    for callback in CALLBACKS[1:]: monkeypatch.setattr(poller, callback, good)
    with caplog.at_level(logging.WARNING, logger="pulse402.poller"):
        asyncio.run(poller.run_single_poll_cycle(None))
    details = poller.get_feed_diagnostics("base-liquidity")
    assert details["last_acquisition_error_code"] == "GOPLUS_API_REJECTED"
    assert details["last_acquisition_http_status"] == 200
    assert details["last_acquisition_api_code"] == expected
    assert SECRET not in caplog.text


def test_health_provider_schema_has_only_fixed_names_and_safe_fields(monkeypatch):
    import app
    from engine import provider_reads
    monkeypatch.setattr(provider_reads, "_DIAGNOSTICS", {})
    provider_reads.provider_error("goplus", "API_REJECTED", status=200, api_code=4029)
    provider_reads.provider_error("dexscreener", "HTTP_429", status=429)
    # Future internal debugging fields must not silently become public fields.
    provider_reads._DIAGNOSTICS["goplus"]["raw_body"] = SECRET
    status = asyncio.run(app.health_check())["public_provider_status"]
    assert set(status) == {"dexscreener", "goplus"}
    expected = {"request_count", "retry_count", "last_failure_code", "last_failure_http_status", "last_failure_api_code", "last_failure_at_ms", "last_success_at_ms"}
    assert all(set(row) == expected for row in status.values())
    assert status["goplus"]["last_failure_api_code"] == 4029
    assert status["dexscreener"]["last_failure_http_status"] == 429
    assert SECRET not in json.dumps(status)


def test_request_info_logging_is_suppressed_without_disabling_warnings(caplog):
    import app  # noqa: F401 -- configures only request-level logger thresholds.
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
    with caplog.at_level(logging.DEBUG):
        for name in ("httpx", "httpcore"):
            logging.getLogger(name).info("URL %s", SECRET)
            logging.getLogger(name).warning("safe warning")
    assert SECRET not in caplog.text
    assert sum(record.message == "safe warning" for record in caplog.records) == 2
