"""Known unfunded signatures, fake time and durable temporary payment state only."""
import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from engine import payments
import test_payments as fixtures


class PaidDeliveryFreshnessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {
            "PULSE_PAYMENTS_DB": str(Path(self.tmp.name) / "payments.sqlite3"),
            "PAYOUT_WALLET_ADDRESS": "0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6",
            "SOLANA_MERCHANT_ADDRESS": "9wuMysgXwNhtQeqJgGByAiMYc4EjzZ5wUSmnjrrt3ffh",
        })
        self.env.start()
        self.now = time.time()
        self.clock = patch.object(payments.time, "time", side_effect=lambda: self.now)
        self.clock.start()
        self.fac = AsyncMock(side_effect=self.settle)
        self.fac_patch = patch.object(payments, "facilitator", self.fac)
        self.fac_patch.start()
        self.payment = fixtures.base_payment(nonce=700)
        self.raw = payments.canonical(self.payment)
        self.identity = payments.owned_payment(self.raw, fixtures.PATH, "5000")[2]

    async def asyncTearDown(self):
        self.fac_patch.stop()
        self.clock.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def settle(self, action, envelope):
        if action == "verify":
            return {"isValid": True}
        self.assertEqual(action, "settle")
        return fixtures.successful_settlement(envelope["paymentPayload"])

    def snapshot(self, label="fresh", age_ms=0):
        return {"status": "ok", "timestamp_ms": int(self.now * 1000) - age_ms,
                "ttl_seconds": 10, "data": [{"symbol": "WETH", "label": label}]}

    def valid(self, value):
        return (isinstance(value, dict) and type(value.get("timestamp_ms")) is int
                and value.get("ttl_seconds") == 10
                and -1000 <= int(self.now * 1000) - value["timestamp_ms"] < 9000)

    async def purchase(self, acquire, **kwargs):
        return await payments.execute_purchase(self.raw, fixtures.PATH, {}, acquire,
                                               delivery_valid=self.valid, **kwargs)

    def row(self):
        with payments.database() as db:
            result = db.execute("SELECT * FROM purchases WHERE id=?", (self.identity,)).fetchone()
            return dict(result) if result else None

    async def make_paid_pending(self):
        first = self.snapshot("prepared", 1000)
        async def delayed(action, envelope):
            if action == "verify":
                return {"isValid": True}
            self.now += 10
            return fixtures.successful_settlement(envelope["paymentPayload"])
        self.fac.side_effect = delayed
        acquire = AsyncMock(side_effect=[first, RuntimeError("PRIVATE_PROVIDER_ERROR")])
        with self.assertRaises(payments.PaymentError) as raised:
            await self.purchase(acquire)
        self.assertIs(raised.exception.charged, True)
        self.assertEqual(raised.exception.code, "PAID_FULFILLMENT_PENDING")
        self.assertEqual(self.row()["state"], "paid_pending_fulfillment")
        self.assertIsNone(self.row()["owner"])
        self.fac.side_effect = self.settle
        return json.loads(self.row()["receipt"])

    async def test_stale_prepared_result_never_settles_and_releases_unpaid_row(self):
        with self.assertRaises(payments.PaymentError) as raised:
            await self.purchase(AsyncMock(return_value=self.snapshot(age_ms=9500)))
        self.assertEqual(raised.exception.code, "FULFILLMENT_EXPIRED_BEFORE_SETTLEMENT")
        self.assertIs(raised.exception.charged, False)
        self.assertEqual([call.args[0] for call in self.fac.await_args_list], ["verify"])
        self.assertIsNone(self.row())

    async def test_settlement_expiry_reacquires_actual_data_with_same_receipt_one_charge(self):
        for index, payment in enumerate((self.payment, fixtures.solana_payment(unique=701))):
            with self.subTest(chain=payment["accepted"]["network"]):
                self.raw = payments.canonical(payment)
                self.identity = payments.owned_payment(self.raw, fixtures.PATH, "5000")[2]
                first = self.snapshot("prepared-" + str(index), 2000)
                count = 0
                async def acquire():
                    nonlocal count
                    count += 1
                    if count == 1:
                        return first
                    row = self.row()
                    self.assertEqual(row["state"], "paid_pending_fulfillment")
                    self.assertTrue(json.loads(row["receipt"])["success"])
                    self.assertEqual(json.loads(row["result"]), first)
                    return self.snapshot("new-evidence-" + str(index))
                async def delayed(action, envelope):
                    if action == "verify":
                        return {"isValid": True}
                    self.now += 9
                    return fixtures.successful_settlement(envelope["paymentPayload"])
                self.fac.side_effect = delayed
                self.fac.reset_mock()
                result, receipt, replayed = await self.purchase(acquire)
                self.assertFalse(replayed)
                self.assertEqual(result["data"][0]["label"], "new-evidence-" + str(index))
                self.assertNotEqual(result["timestamp_ms"], first["timestamp_ms"])
                self.assertTrue(self.valid(result))
                self.assertEqual(self.row()["state"], "delivered")
                self.assertEqual(json.loads(self.row()["receipt"]), receipt)
                self.assertEqual([call.args[0] for call in self.fac.await_args_list], ["verify", "settle"])
                with payments.database() as db:
                    archived = db.execute("SELECT result FROM purchase_delivery_history WHERE purchase_id=?", (self.identity,)).fetchone()
                self.assertEqual(json.loads(archived[0]), first)

    async def test_failed_post_settlement_refresh_is_paid_pending_and_expired_auth_recovers_without_facilitator(self):
        receipt = await self.make_paid_pending()
        self.now = int(self.payment["payload"]["authorization"]["validBefore"]) + 100
        self.fac.reset_mock()
        self.fac.side_effect = AssertionError("Recovery must not verify or settle")
        result, recovered_receipt, replayed = await self.purchase(AsyncMock(side_effect=lambda: self.snapshot("recovered")))
        self.assertTrue(replayed)
        self.assertEqual(recovered_receipt, receipt)
        self.assertEqual(result["data"][0]["label"], "recovered")
        self.fac.assert_not_awaited()
        self.assertEqual(self.row()["state"], "delivered")

    async def test_concurrent_recovery_is_exclusive_and_old_expired_owner_cannot_overwrite_winner(self):
        receipt = await self.make_paid_pending()
        self.fac.reset_mock()
        entered, release = asyncio.Event(), asyncio.Event()
        async def paused():
            entered.set()
            await release.wait()
            return self.snapshot("late-old-owner")
        first = asyncio.create_task(self.purchase(paused))
        await asyncio.wait_for(entered.wait(), 2)
        never_acquire = AsyncMock()
        with self.assertRaises(payments.PaymentError) as concurrent:
            await self.purchase(never_acquire)
        self.assertEqual(concurrent.exception.code, "PAID_FULFILLMENT_IN_PROGRESS")
        never_acquire.assert_not_awaited()
        self.now += payments.FULFILLMENT_LEASE_SECONDS + 1
        winner = await self.purchase(AsyncMock(side_effect=lambda: self.snapshot("winner")))
        release.set()
        with self.assertRaises(payments.PaymentError) as stale:
            await first
        self.assertEqual(stale.exception.code, "PAID_FULFILLMENT_SUPERSEDED")
        self.assertIs(stale.exception.charged, True)
        self.assertEqual(winner[1], receipt)
        self.assertEqual(json.loads(self.row()["result"])["data"][0]["label"], "winner")
        self.fac.assert_not_awaited()

    async def test_cancel_after_confirmed_settlement_retains_receipt_and_releases_fulfillment_owner(self):
        await self.make_paid_pending()
        entered = asyncio.Event()
        async def wait_for_cancel():
            entered.set()
            await asyncio.Event().wait()
        self.fac.reset_mock()
        task = asyncio.create_task(self.purchase(wait_for_cancel))
        await asyncio.wait_for(entered.wait(), 2)
        receipt = self.row()["receipt"]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.row()["state"], "paid_pending_fulfillment")
        self.assertEqual(self.row()["receipt"], receipt)
        self.assertIsNone(self.row()["owner"])
        self.fac.assert_not_awaited()

    async def test_post_settlement_refresh_timeout_is_bounded_and_does_not_resettle(self):
        await self.make_paid_pending()
        self.fac.reset_mock()
        async def never_finishes():
            await asyncio.Event().wait()
        with patch.object(payments, "FULFILLMENT_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(payments.PaymentError) as raised:
                await self.purchase(never_finishes)
        self.assertIs(raised.exception.charged, True)
        self.assertEqual(self.row()["state"], "paid_pending_fulfillment")
        self.assertIsNone(self.row()["owner"])
        self.fac.assert_not_awaited()

    async def test_persisting_confirmed_receipt_failure_stops_unknown_and_never_resettles(self):
        original = payments.database
        class FailingConnection:
            def __init__(self, db): self.db = db
            def __getattr__(self, name): return getattr(self.db, name)
            def execute(self, sql, parameters=()):
                if "SET state='paid_pending_fulfillment',receipt=" in sql:
                    raise sqlite3.OperationalError("OFFLINE_DISK_FAILURE")
                return self.db.execute(sql, parameters)
        @contextmanager
        def failing_database():
            with original() as db:
                yield FailingConnection(db)
        with patch.object(payments, "database", failing_database):
            with self.assertRaises(payments.PaymentError) as raised:
                await self.purchase(AsyncMock(side_effect=lambda: self.snapshot()))
        self.assertEqual(raised.exception.charged, "unknown")
        self.assertEqual(self.row()["state"], "settling")
        self.fac.reset_mock()
        with self.assertRaises(payments.PaymentError):
            await self.purchase(AsyncMock(side_effect=lambda: self.snapshot()))
        self.fac.assert_not_awaited()

    async def test_operator_repair_archives_exact_legacy_expired_at_delivery_and_reuses_receipt(self):
        original = self.snapshot("legacy-original", 9500)
        receipt = payments.receipt_for(fixtures.successful_settlement(self.payment), self.payment["accepted"], fixtures.PAYER, fixtures.PATH)
        old_updated = (original["timestamp_ms"] + 10_339) / 1000
        with payments.database() as db:
            db.execute("INSERT INTO purchases VALUES(?,?,'delivered','old-owner',?,?,?)",
                       (self.identity, payments.digest({"path": fixtures.PATH, "arguments": {}}), old_updated, payments.canonical(original), payments.canonical(receipt)))
        self.now += 1000
        self.fac.reset_mock()
        result, actual_receipt, replayed = await payments.repair_confirmed_delivery(
            self.identity, fixtures.PATH, {}, AsyncMock(side_effect=lambda: self.snapshot("fixed-delivery")), delivery_valid=self.valid)
        self.assertTrue(replayed)
        self.assertEqual(actual_receipt, receipt)
        self.assertEqual(result["data"][0]["label"], "fixed-delivery")
        with payments.database() as db:
            archived = db.execute("SELECT * FROM purchase_delivery_history WHERE purchase_id=? AND reason='EXPIRED_AT_ORIGINAL_DELIVERY'", (self.identity,)).fetchone()
        self.assertEqual(archived["result"], payments.canonical(original))
        self.assertEqual(archived["receipt"], payments.canonical(receipt))
        self.assertEqual(archived["updated"], old_updated)
        self.assertEqual(archived["state"], "delivered")
        self.fac.assert_not_awaited()

    async def test_successful_historical_replay_stays_original_despite_later_expiry(self):
        original, receipt, _ = await self.purchase(AsyncMock(side_effect=lambda: self.snapshot("original-success")))
        self.now += 1000
        self.fac.reset_mock()
        no_acquisition = AsyncMock()
        replay = await self.purchase(no_acquisition)
        self.assertEqual(replay, (original, receipt, True))
        no_acquisition.assert_not_awaited()
        self.fac.assert_not_awaited()
        with payments.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM purchase_delivery_history").fetchone()[0], 0)

    async def test_operator_recovery_rejects_changed_fingerprint_and_unconfirmed_state(self):
        await self.make_paid_pending()
        self.fac.reset_mock()
        for changed_arguments in ({"symbol": "AERO"},):
            with self.assertRaises(payments.PaymentError) as conflict:
                await payments.repair_confirmed_delivery(self.identity, fixtures.PATH, changed_arguments, AsyncMock(), delivery_valid=self.valid)
            self.assertEqual(conflict.exception.code, "PAYMENT_REQUEST_CONFLICT")
        with payments.database() as db:
            db.execute("UPDATE purchases SET state='unknown' WHERE id=?", (self.identity,))
        with self.assertRaises(payments.PaymentError) as unknown:
            await payments.repair_confirmed_delivery(self.identity, fixtures.PATH, {}, AsyncMock(), delivery_valid=self.valid)
        self.assertEqual(unknown.exception.charged, "unknown")
        self.fac.assert_not_awaited()

    async def test_unknown_settlement_and_reconciliation_storage_failure_remain_charged_unknown(self):
        original_database = payments.database
        class FailingConnection:
            def __init__(self, db): self.db = db
            def __getattr__(self, name): return getattr(self.db, name)
            def execute(self, sql, parameters=()):
                if "SET state='unknown'" in sql:
                    raise sqlite3.OperationalError("OFFLINE_RECONCILIATION_DISK_FAILURE")
                return self.db.execute(sql, parameters)
        @contextmanager
        def failing_database():
            with original_database() as db:
                yield FailingConnection(db)
        async def ambiguous(action, envelope):
            if action == "verify":
                return {"isValid": True}
            raise TimeoutError("Unknown remote settlement outcome")
        self.fac.side_effect = ambiguous
        with patch.object(payments, "database", failing_database):
            with self.assertRaises(payments.PaymentError) as raised:
                await self.purchase(AsyncMock(side_effect=lambda: self.snapshot()))
        self.assertEqual(raised.exception.code, "PAYMENT_RECOVERY_REQUIRED")
        self.assertEqual(raised.exception.charged, "unknown")
        self.assertEqual(self.row()["state"], "settling")
        self.fac.reset_mock()
        with self.assertRaises(payments.PaymentError):
            await self.purchase(AsyncMock())
        self.fac.assert_not_awaited()

    async def test_corrupt_paid_result_never_exposes_untyped_parse_error_or_resettles(self):
        await self.make_paid_pending()
        with payments.database() as db:
            db.execute("UPDATE purchases SET result='not JSON' WHERE id=?", (self.identity,))
        self.fac.reset_mock()
        with self.assertRaises(payments.PaymentError) as corrupt:
            await self.purchase(AsyncMock())
        self.assertEqual(corrupt.exception.code, "PAYMENT_RECOVERY_REQUIRED")
        self.assertEqual(corrupt.exception.charged, "unknown")
        self.fac.assert_not_awaited()

    async def test_operator_recovery_requires_complete_explicit_normalized_receipt(self):
        receipt = await self.make_paid_pending()
        self.fac.reset_mock()
        for field in ("amount", "resource", "network", "payer", "transaction"):
            with self.subTest(missing=field):
                invalid = {key: value for key, value in receipt.items() if key != field}
                with payments.database() as db:
                    db.execute("UPDATE purchases SET receipt=? WHERE id=?", (payments.canonical(invalid), self.identity))
                no_acquisition = AsyncMock()
                with self.assertRaises(payments.PaymentError) as rejected:
                    await payments.repair_confirmed_delivery(self.identity, fixtures.PATH, {}, no_acquisition, delivery_valid=self.valid)
                self.assertEqual(rejected.exception.code, "INVALID_RECOVERY_RECEIPT")
                self.assertEqual(rejected.exception.charged, "unknown")
                no_acquisition.assert_not_awaited()
        self.fac.assert_not_awaited()

    async def test_expired_session_result_never_consumes_credit_and_completed_replay_is_unchanged(self):
        token = "pulse_sess_" + "A" * 43
        token_hash = payments.hashlib.sha256(token.encode()).hexdigest()
        with payments.database() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,6000,'known-fixture-purchase')", (token_hash, self.now + 5000))
        with self.assertRaises(payments.PaymentError) as expired:
            await payments.execute_session(token, "expired", fixtures.PATH, {}, AsyncMock(return_value=self.snapshot(age_ms=9500)), delivery_valid=self.valid)
        self.assertIs(expired.exception.charged, False)
        with payments.database() as db:
            self.assertEqual(db.execute("SELECT remaining FROM sessions").fetchone()[0], 6000)
            self.assertEqual(db.execute("SELECT count(*) FROM credit_deliveries").fetchone()[0], 0)
        first = await payments.execute_session(token, "success", fixtures.PATH, {}, AsyncMock(side_effect=lambda: self.snapshot()), delivery_valid=self.valid)
        self.now += 100
        never = AsyncMock()
        replay = await payments.execute_session(token, "success", fixtures.PATH, {}, never, delivery_valid=self.valid)
        self.assertEqual(first[0], replay[0])
        self.assertEqual(replay[1], 5999)
        self.assertTrue(replay[3])
        never.assert_not_awaited()
        self.fac.assert_not_awaited()
