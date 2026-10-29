"""Connection ownership and fresh cycle boundaries; no network or payments."""
import asyncio
from types import SimpleNamespace

import pytest

from engine import base_dex, poller


class Client:
    is_closed = False

    def __init__(self, order):
        self.order = order

    async def __aenter__(self):
        self.order.append("opened")
        return self

    async def __aexit__(self, *args):
        self.is_closed = True
        self.order.append("closed")


def test_reused_connection_client_still_creates_and_releases_distinct_quote_cycles(monkeypatch):
    order, created, cycles = [], [], []
    client = Client(order)
    monkeypatch.setattr(poller, "_POLL_CLIENT", client)
    for name, value in (("_CYCLE", None), ("_CYCLE_TASKS", {}), ("_CLIENT_CYCLES", {})):
        monkeypatch.setattr(base_dex, name, value)

    async def new_cycle(owner):
        assert owner is client and not owner.is_closed
        value = SimpleNamespace(client=owner, observed_at_ms=1, sequence=len(created))
        created.append(value)
        return value

    async def acquire(owner):
        cycles.append(await base_dex.acquire_quote_cycle(owner))

    monkeypatch.setattr(base_dex, "_new_cycle", new_cycle)
    monkeypatch.setattr(poller, "run_single_poll_cycle", acquire)

    async def run():
        await poller._refresh()
        assert base_dex._CYCLE is None and not base_dex._CLIENT_CYCLES
        await poller._refresh()
        assert base_dex._CYCLE is None and not base_dex._CLIENT_CYCLES

    asyncio.run(run())
    assert cycles == created and len(created) == 2 and cycles[0] is not cycles[1]
    assert not client.is_closed  # The background owner, not each cycle, closes it.


def test_standalone_refresh_closes_its_client_after_releasing_quotes(monkeypatch):
    order = []
    client = Client(order)
    monkeypatch.setattr(poller, "_POLL_CLIENT", None)
    monkeypatch.setattr(poller, "_feed_client", lambda: client)

    async def acquire(owner):
        assert owner is client and not owner.is_closed
        order.append("acquired")

    async def release(owner):
        assert owner is client and not owner.is_closed
        order.append("released")

    monkeypatch.setattr(poller, "run_single_poll_cycle", acquire)
    monkeypatch.setattr(poller, "release_quote_cycle", release)
    asyncio.run(poller._refresh())
    assert order == ["opened", "acquired", "released", "closed"]


def test_background_cancellation_drains_shielded_acquisition_before_closing_pool(monkeypatch):
    order = []
    client = Client(order)
    monkeypatch.setattr(poller, "_POLL_CLIENT", None)
    monkeypatch.setattr(poller, "_REFRESH_TASK", None)
    monkeypatch.setattr(poller, "_feed_client", lambda: client)

    async def run():
        entered = asyncio.Event()

        async def acquire(owner):
            assert owner is client
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                assert not client.is_closed
                order.append("acquisition_drained")

        async def release(owner):
            assert owner is client and not owner.is_closed
            order.append("quotes_released")

        monkeypatch.setattr(poller, "run_single_poll_cycle", acquire)
        monkeypatch.setattr(poller, "release_quote_cycle", release)
        task = asyncio.create_task(poller.start_poller_loop())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert poller._POLL_CLIENT is None and poller._REFRESH_TASK is None

    asyncio.run(run())
    assert order == ["opened", "acquisition_drained", "quotes_released", "closed"]


def test_app_lifespan_awaits_poller_cleanup(monkeypatch):
    import app

    async def run():
        entered, cleaned = asyncio.Event(), asyncio.Event()

        async def background(**kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()

        monkeypatch.setattr(app, "start_poller_loop", background)
        async with app.lifespan(app.app):
            await asyncio.wait_for(entered.wait(), 1)
        assert cleaned.is_set()

    asyncio.run(run())


def test_second_background_owner_is_rejected_without_closing_the_first(monkeypatch):
    client = Client([])
    monkeypatch.setattr(poller, "_POLL_CLIENT", client)
    with pytest.raises(RuntimeError, match="already has an owner"):
        asyncio.run(poller.start_poller_loop())
    assert not client.is_closed
