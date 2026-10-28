"""Local Actor/engine stubs only; no platform calls, payment, signing or credentials."""
import asyncio
import copy
from decimal import Decimal
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

NOW = 2_000_000.0


class ActorStub:
    def __init__(self):
        self.input = {"feed": "all"}
        self.log = Mock()
        self.data = []
        self.charged = []
        self.sequence = []
        self.budget = None
        self.ppe = True
        self.prices = {"oracle-feed-fetch": Decimal("0.02")}
        self.storage_error = None
        self.billing_error = None
        self.after_push = lambda: None
        self.charge = AsyncMock(side_effect=AssertionError("Actor must use the charged push shortcut, never a separate charge"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get_input(self):
        return self.input

    def get_charging_manager(self):
        return self

    def get_pricing_info(self):
        return SimpleNamespace(is_pay_per_event=self.ppe, per_event_prices=self.prices)

    def calculate_max_event_charge_count_within_limit(self, event_name):
        assert event_name == "oracle-feed-fetch"
        return self.budget

    async def push_data(self, data, *, charged_event_name):
        # Match verified SDK ordering: cap writes to budget, persist then charge.
        assert charged_event_name == "oracle-feed-fetch"
        if self.budget is not None and self.budget <= 0:
            return SimpleNamespace(charged_count=0, event_charge_limit_reached=True)
        if self.storage_error:
            raise self.storage_error
        self.sequence.append(("stored", data["feed"]))
        self.data.append(copy.deepcopy(data))
        if self.billing_error:
            self.sequence.append(("billing_error", data["feed"]))
            raise self.billing_error
        self.sequence.append(("charged", data["feed"]))
        self.charged.append(charged_event_name)
        if self.budget is not None:
            self.budget -= 1
        self.after_push()
        return SimpleNamespace(charged_count=1, event_charge_limit_reached=self.budget == 0)


def snapshot(feed, stamp=None):
    stamp = int(NOW * 1000) - 10 if stamp is None else stamp
    data = [{"symbol": "OWNER_FULL_FEED_FIXTURE", "timestamp_ms": stamp, "evidence": {"source": "unsigned fixture provider", "block_hash": "fixture-original-block"}}]
    if feed == "threat-intel":
        data = {"monitored_token_assessments": [{"token_address": "0x" + "ab" * 20, "observed_at_ms": stamp}], "active_malicious_spenders": [], "timestamp_ms": stamp}
    return {"status": "ok", "feed": feed, "timestamp_ms": stamp, "ttl_seconds": 10, "data": data}


@pytest.fixture
def actor(monkeypatch):
    fake = ActorStub()
    monkeypatch.setitem(sys.modules, "apify", SimpleNamespace(Actor=fake))
    target = Path(__file__).parents[1] / "apify" / "apify_main.py"
    spec = importlib.util.spec_from_file_location("pulse_actor_delivery_fixture", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = [NOW]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    acquired = AsyncMock(side_effect=lambda feed: snapshot(feed))
    monkeypatch.setattr(module.poller, "acquire_feed_state", acquired)
    # Any unintended external backend/provider request is a test failure.
    monkeypatch.setattr(httpx.AsyncClient, "get", AsyncMock(side_effect=AssertionError("No HTTP endpoint may be called in actor tests")))
    monkeypatch.setattr(httpx.AsyncClient, "post", AsyncMock(side_effect=AssertionError("No payment or other POST may be called in actor tests")))
    return module, fake, acquired, clock


@pytest.mark.parametrize("choice,feed", [("base_liquidity", "base-liquidity"), ("polymarket_velocity", "polymarket-velocity"), ("threat_intel", "threat-intel"), ("arb_routes", "arb-routes")])
def test_each_choice_delivers_original_full_local_feed_then_one_event(actor, choice, feed):
    module, fake, acquired, _ = actor
    fake.input = {"feed": choice}
    asyncio.run(module.main())
    acquired.assert_awaited_once_with(feed)
    assert fake.data == [snapshot(feed)]
    assert fake.sequence == [("stored", feed), ("charged", feed)]
    assert fake.charged == ["oracle-feed-fetch"]
    fake.charge.assert_not_awaited()


def test_default_all_delivers_four_full_snapshots_without_charging_per_record(actor):
    module, fake, acquired, _ = actor
    fake.input = None
    asyncio.run(module.main())
    assert {call.args[0] for call in acquired.await_args_list} == set(module.FEEDS.values())
    assert len(fake.data) == len(fake.charged) == 4
    for delivered in fake.data:
        assert delivered == snapshot(delivered["feed"])
        assert delivered["timestamp_ms"] == int(NOW * 1000) - 10


@pytest.mark.parametrize("value", [[], "all", {"feed": "wrong"}, {"feed": None}, {"feed": []}, {"feed": True}, {"url": "https://untrusted.example"}])
def test_invalid_input_never_acquires_pushes_or_charges(actor, value):
    module, fake, acquired, _ = actor
    fake.input = value
    with pytest.raises(ValueError):
        asyncio.run(module.main())
    acquired.assert_not_awaited()
    assert fake.data == fake.charged == []


@pytest.mark.parametrize("change", [
    {"status": "stale"}, {"status": "unavailable"}, {"status": "error"}, {"status": "preview"}, {"error": "provider-failed"},
    {"feed": "other-feed"}, {"timestamp_ms": None}, {"timestamp_ms": True}, {"timestamp_ms": int(NOW * 1000) - 10_000},
    {"timestamp_ms": int(NOW * 1000) + 1001}, {"ttl_seconds": 0}, {"ttl_seconds": True}, {"data": None}, {"data": []},
    {"data": [{}]}, {"data": [{"error": "no-provider"}]}, {"data": [{"status": "stale"}]}, {"data": {"records_available": 100}},
    {"data": [{"value": float("nan")}]}, {"data": [{"value": object()}]}, {"data": [{"text": "x" * 2_000_001}]},
])
def test_unavailable_stale_empty_error_summary_and_unserializable_results_never_bill(actor, change):
    module, fake, acquired, _ = actor
    fake.input = {"feed": "base_liquidity"}
    acquired.side_effect = lambda feed: {**snapshot(feed), **change}
    asyncio.run(module.main())
    assert fake.data == fake.charged == []
    fake.charge.assert_not_awaited()


def test_threat_summary_without_monitored_assessments_is_not_a_paid_feed(actor):
    module, fake, acquired, _ = actor
    fake.input = {"feed": "threat_intel"}
    acquired.side_effect = lambda feed: {**snapshot(feed), "data": {"monitored_token_assessments": [], "coverage": {"requested_tokens": 5}}}
    asyncio.run(module.main())
    assert fake.data == fake.charged == []


@pytest.mark.parametrize("error", [RuntimeError("provider failed"), asyncio.TimeoutError()])
def test_acquisition_exception_or_timeout_never_pushes_or_charges(actor, error):
    module, fake, acquired, _ = actor
    fake.input = {"feed": "base_liquidity"}
    acquired.side_effect = error
    asyncio.run(module.main())
    assert fake.data == fake.charged == []


def test_one_failed_feed_does_not_convert_remaining_full_deliveries_to_previews(actor):
    module, fake, acquired, _ = actor
    def acquire(feed):
        if feed == "base-liquidity":
            raise RuntimeError("provider failed")
        return snapshot(feed)
    acquired.side_effect = acquire
    asyncio.run(module.main())
    assert len(fake.data) == len(fake.charged) == 3
    assert all(item["feed"] != "base-liquidity" for item in fake.data)


def test_queued_snapshot_that_ages_out_during_storage_is_not_restamped_or_charged(actor):
    module, fake, _, clock = actor
    fake.after_push = lambda: clock.__setitem__(0, NOW + 11)
    asyncio.run(module.main())
    assert len(fake.data) == len(fake.charged) == 1
    assert fake.data[0]["timestamp_ms"] == int(NOW * 1000) - 10


@pytest.mark.parametrize("budget,expected", [(0, 0), (1, 1), (2, 2)])
def test_ppe_budget_caps_full_feed_deliveries_and_avoids_zero_budget_acquisition(actor, budget, expected):
    module, fake, acquired, _ = actor
    fake.budget = budget
    asyncio.run(module.main())
    assert len(fake.data) == len(fake.charged) == expected
    if budget == 0:
        acquired.assert_not_awaited()


@pytest.mark.parametrize("ppe,prices", [(False, {"oracle-feed-fetch": Decimal("0.02")}), (True, {}), (True, {"oracle-feed-fetch": Decimal(0)}), (True, {"oracle-feed-fetch": Decimal("NaN")})])
def test_missing_or_free_ppe_configuration_fails_before_provider_or_delivery(actor, ppe, prices):
    module, fake, acquired, _ = actor
    fake.ppe, fake.prices = ppe, prices
    with pytest.raises(RuntimeError):
        asyncio.run(module.main())
    acquired.assert_not_awaited()
    assert fake.data == fake.charged == []


def test_old_sdk_cannot_silently_deliver_without_billing(actor):
    module, fake, acquired, _ = actor
    async def old_push(data):
        raise AssertionError("Old uncharged push must not run")
    fake.push_data = old_push
    with pytest.raises(RuntimeError, match="charged push_data"):
        asyncio.run(module.main())
    acquired.assert_not_awaited()


@pytest.mark.parametrize("stage", ["storage", "billing"])
def test_ambiguous_push_is_not_retried_or_followed_by_separate_charge(actor, stage):
    module, fake, _, _ = actor
    fake.input = {"feed": "base_liquidity"}
    if stage == "storage":
        fake.storage_error = RuntimeError("dataset write failure")
    else:
        fake.billing_error = RuntimeError("billing acknowledgment failure")
    with pytest.raises(RuntimeError):
        asyncio.run(module.main())
    assert len(fake.data) == (0 if stage == "storage" else 1)
    assert fake.charged == []
    fake.charge.assert_not_awaited()
