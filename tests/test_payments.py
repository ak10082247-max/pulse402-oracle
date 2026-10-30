"""Unfunded signature fixtures and isolated transports; no live settlement/key files."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from eth_account import Account
from eth_account.messages import encode_typed_data
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import app as web
from engine import payments

FIXTURE_KEY = bytes.fromhex("06" * 32)  # deterministic unfunded test key only
PAYER = Account.from_key(FIXTURE_KEY).address
PATH = "/v1/pulse/base-liquidity"
PRIVATE_RECORD = {"symbol": "WETH", "private_marker": "FULL_PAID_RECORD_OWNER_FIXTURE"}


def base_payment(path=PATH, *, amount="5000", nonce=1, valid_before=None, valid_after=0):
    quote = payments.requirements(amount)[0]
    auth = {"from": PAYER, "to": quote["payTo"], "value": amount, "validAfter": str(valid_after), "validBefore": str(valid_before or int(time.time()) + 180), "nonce": "0x" + format(nonce, "064x")}
    fields = [("from", "address"), ("to", "address"), ("value", "uint256"), ("validAfter", "uint256"), ("validBefore", "uint256"), ("nonce", "bytes32")]
    typed = {"types": {"EIP712Domain": [{"name": n, "type": t} for n, t in [("name", "string"), ("version", "string"), ("chainId", "uint256"), ("verifyingContract", "address")]], "TransferWithAuthorization": [{"name": n, "type": t} for n, t in fields]}, "primaryType": "TransferWithAuthorization", "domain": {"name": "USD Coin", "version": "2", "chainId": 8453, "verifyingContract": payments.USDC}, "message": {k: auth[k] if t in ("address", "bytes32") else int(auth[k]) for k, t in fields}}
    signature = "0x" + Account.sign_message(encode_typed_data(full_message=typed), FIXTURE_KEY).signature.hex().removeprefix("0x")
    return {"x402Version": 2, "accepted": quote, "resource": {"url": payments.PUBLIC_URL + path}, "payload": {"authorization": auth, "signature": signature}}


def solana_payment(path=PATH, *, amount="5000", unique=1):
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex("07" * 32))
    buyer = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    quote = payments.requirements(amount)[1]
    # Real sponsored-v0 layout: CU limit, CU price, USDC TransferChecked. Account
    # addresses and blockhash are unfunded synthetic fixtures, never submitted.
    keys = [payments.b58decode(payments.FEE_PAYER), buyer, hashlib.sha256(b"unfunded owner source ATA").digest(), payments.b58decode("DD3A7jaXFeHcif9eYj6Vhomuxypat5Yxi8ow6wxz27yL"), payments.b58decode(payments.SOL_USDC), payments.b58decode("ComputeBudget111111111111111111111111111111"), payments.b58decode("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")]
    compute_limit = bytes([5, 0, 5, 2]) + (20000).to_bytes(4, "little")
    compute_price = bytes([5, 0, 9, 3]) + (1).to_bytes(8, "little")
    transfer = bytes([6, 4, 2, 4, 3, 1, 10, 12]) + int(amount).to_bytes(8, "little") + bytes([6])
    message = bytes([128, 2, 0, 3, len(keys)]) + b"".join(keys) + hashlib.sha256(str(unique).encode()).digest() + bytes([3]) + compute_limit + compute_price + transfer + bytes([0])
    wire = bytes([2]) + bytes(64) + key.sign(message) + message
    return {"x402Version": 2, "accepted": quote, "resource": {"url": payments.PUBLIC_URL + path}, "payload": {"transaction": base64.b64encode(wire).decode()}}


def encoded(payment):
    return base64.b64encode(json.dumps(payment).encode()).decode()


def successful_settlement(payment):
    quote = payment["accepted"]
    _, _, _, payer = payments.owned_payment(payments.canonical(payment), payment["resource"]["url"].removeprefix(payments.PUBLIC_URL), quote["amount"])
    tx = "0x" + "ab" * 32 if quote["network"] == payments.BASE else payments.b58encode(bytes.fromhex("08" * 64))
    return {"success": True, "transaction": tx, "payer": payer, "network": quote["network"], "amount": quote["amount"]}


class PaymentGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"PULSE_PAYMENTS_DB": str(Path(self.tmp.name) / "payments.sqlite3"), "PAYOUT_WALLET_ADDRESS": "0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6", "SOLANA_MERCHANT_ADDRESS": "9wuMysgXwNhtQeqJgGByAiMYc4EjzZ5wUSmnjrrt3ffh"})
        self.env.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="https://pulse402-oracle.onrender.com")
        self.provider = patch.object(web, "acquire_feed", AsyncMock(return_value={"status": "ok", "timestamp_ms": int(time.time() * 1000), "ttl_seconds": 10, "data": [PRIVATE_RECORD]}))
        self.acquire = self.provider.start()
        # HTTP ASGI calls need post(), so isolate facilitator instead of replacing
        # shared HTTP methods. Every test controls this mock; no public endpoint runs.
        self.facilitator = patch.object(payments, "facilitator", AsyncMock(side_effect=self.stub_facilitator))
        self.fac = self.facilitator.start()

    async def asyncTearDown(self):
        await self.client.aclose()
        self.facilitator.stop()
        self.provider.stop()
        self.env.stop()
        self.tmp.cleanup()

    async def _no_external_post(self, *args, **kwargs):
        self.fail("No live external request is permitted")

    async def stub_facilitator(self, action, envelope):
        if action == "verify":
            return {"isValid": True}
        if action == "settle":
            return successful_settlement(envelope["paymentPayload"])
        self.fail("Unexpected facilitator action")

    def assert_no_private_record(self, response):
        self.assertNotIn(PRIVATE_RECORD["private_marker"], response.text)
        self.assertNotIn("session_token", response.text)

    async def test_unsigned_http_mcp_sanitize_and_prepay_cannot_deliver(self):
        requests = [("GET", PATH, None), ("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "pulse_base_liquidity", "arguments": {}}}), ("POST", "/v1/pulse/threat-intel/sanitize", {"content": "owner fixture"}), ("POST", "/v1/pass/prepay", {})]
        for method, path, body in requests:
            result = await self.client.request(method, path, json=body)
            self.assertEqual(result.status_code, 402)
            self.assertIn("payment-required", result.headers)
            self.assert_no_private_record(result)
        self.fac.assert_not_awaited()
        self.acquire.assert_not_awaited()

    async def test_garbage_headers_never_unlock_any_paid_surface(self):
        bodies = [(PATH, "GET", None), ("/mcp", "POST", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "pulse_base_liquidity", "arguments": {}}}), ("/v1/pulse/threat-intel/sanitize", "POST", {"content": "fixture"}), ("/v1/pass/prepay", "POST", {})]
        for raw in ("anything", "test_sig", "0x" + "ab" * 32, "e30=", "null", base64.b64encode(b'"authorized"').decode()):
            for path, method, body in bodies:
                result = await self.client.request(method, path, json=body, headers={"PAYMENT-SIGNATURE": raw})
                self.assertIn(result.status_code, (400, 402))
                self.assert_no_private_record(result)
        self.fac.assert_not_awaited()
        self.acquire.assert_not_awaited()

    async def test_base_and_solana_actual_fixture_signatures_deliver_receipts(self):
        for payment in (base_payment(nonce=10), solana_payment(unique=10)):
            result = await self.client.get(PATH, headers={"PAYMENT-SIGNATURE": encoded(payment)})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["data"], [PRIVATE_RECORD])
            receipt = json.loads(base64.b64decode(result.headers["payment-response"]))
            self.assertTrue(receipt["success"])
            self.assertEqual(receipt["amount"], "5000")
            self.assertEqual(receipt["network"], payment["accepted"]["network"])
            self.assertEqual(receipt["resource"]["url"], payments.PUBLIC_URL + PATH)
        self.assertEqual([call.args[0] for call in self.fac.await_args_list], ["verify", "settle", "verify", "settle"])

    async def test_invalid_buyer_signatures_and_substituted_terms_fail_before_provider(self):
        corrupt = []
        p = base_payment(nonce=11); p["payload"]["signature"] = "0x" + "00" * 65; corrupt.append(p)
        p = solana_payment(unique=11); wire = bytearray(base64.b64decode(p["payload"]["transaction"])); wire[65] ^= 1; p["payload"]["transaction"] = base64.b64encode(wire).decode(); corrupt.append(p)
        for field, value in (("payTo", PAYER), ("amount", "1"), ("network", "eip155:1"), ("maxTimeoutSeconds", 3600)):
            p = base_payment(nonce=12); p["accepted"][field] = value; corrupt.append(p)
        p = base_payment(nonce=13); p["resource"]["url"] += "/wrong"; corrupt.append(p)
        for payment in corrupt:
            result = await self.client.get(PATH, headers={"PAYMENT-SIGNATURE": encoded(payment)})
            self.assertEqual(result.status_code, 402)
            self.assert_no_private_record(result)
        self.fac.assert_not_awaited()
        self.acquire.assert_not_awaited()

    async def test_prepaid_pass_settles_five_usdc_once_before_allocating_6000_credits(self):
        header = {"PAYMENT-SIGNATURE": encoded(base_payment("/v1/pass/prepay", amount="5000000", nonce=20))}
        with payments.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
        first = await self.client.post("/v1/pass/prepay", json={}, headers=header)
        second = await self.client.post("/v1/pass/prepay", json={}, headers=header)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.json()["calls_allocated"], 2500)
        self.assertEqual(first.json()["calls_remaining"], 2500)
        self.assertEqual(json.loads(base64.b64decode(first.headers["payment-response"]))["amount"], "5000000")
        self.assertEqual([call.args[0] for call in self.fac.await_args_list], ["verify", "settle"])
        with payments.database() as db:
            row = db.execute("SELECT remaining,token_hash FROM sessions").fetchone()
        self.assertEqual(row[0], 2500)
        self.assertEqual(row[1], hashlib.sha256(first.json()["session_token"].encode()).hexdigest())

    async def test_bad_prepay_receipt_allocates_no_credit_and_same_auth_never_resettles(self):
        payment = base_payment("/v1/pass/prepay", amount="5000000", nonce=21)
        async def bad(action, envelope):
            return {"isValid": True} if action == "verify" else {"success": True, "transaction": "fabricated", "payer": PAYER, "network": payments.BASE}
        self.fac.side_effect = bad
        first = await self.client.post("/v1/pass/prepay", json={}, headers={"PAYMENT-SIGNATURE": encoded(payment)})
        second = await self.client.post("/v1/pass/prepay", json={}, headers={"PAYMENT-SIGNATURE": encoded(payment)})
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 409)
        self.assert_no_private_record(first)
        self.assertEqual([call.args[0] for call in self.fac.await_args_list], ["verify", "settle"])
        with payments.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT state FROM purchases").fetchone()[0], "unknown")

    async def test_solana_prepay_requires_exact_five_usdc_settlement(self):
        payment = solana_payment("/v1/pass/prepay", amount="5000000", unique=22)
        response = await self.client.post("/v1/pass/prepay", json={}, headers={"PAYMENT-SIGNATURE": encoded(payment)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["calls_allocated"], 2500)
        receipt = json.loads(base64.b64decode(response.headers["payment-response"]))
        self.assertEqual(receipt["network"], payments.SOLANA)
        self.assertEqual(receipt["amount"], "5000000")
        self.assertEqual([call.args[0] for call in self.fac.await_args_list], ["verify", "settle"])

    async def test_all_ambiguous_receipt_fields_quarantine_without_delivering(self):
        for nonce, change in enumerate(({"success": "true"}, {"payer": web.PAYOUT_WALLET_ADDRESS}, {"network": payments.SOLANA}, {"amount": "1"}, {"resource": {"url": "https://other.invalid/path"}}), 120):
            payment = base_payment(nonce=nonce)
            async def malformed(action, envelope):
                return {"isValid": True} if action == "verify" else {**successful_settlement(envelope["paymentPayload"]), **change}
            self.fac.side_effect = malformed
            headers = {"PAYMENT-SIGNATURE": encoded(payment)}
            first = await self.client.get(PATH, headers=headers)
            second = await self.client.get(PATH, headers=headers)
            self.assertEqual(first.status_code, 503)
            self.assertEqual(second.status_code, 409)
            self.assert_no_private_record(first)
            self.assertNotIn("payment-response", first.headers)
        self.assertEqual([call.args[0] for call in self.fac.await_args_list].count("settle"), 5)

    async def test_paid_retry_recovers_after_new_client_and_auth_expiry(self):
        payment = base_payment(nonce=30)
        header = {"PAYMENT-SIGNATURE": encoded(payment)}
        first = await self.client.get(PATH, headers=header)
        await self.client.aclose()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="https://pulse402-oracle.onrender.com")
        with patch.object(payments.time, "time", return_value=int(payment["payload"]["authorization"]["validBefore"]) + 60):
            replay = await self.client.get(PATH, headers=header)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(first.json(), replay.json())
        self.assertEqual(replay.headers["idempotency-replayed"], "true")
        self.assertEqual(self.fac.await_count, 2)
        self.assertEqual(self.acquire.await_count, 1)

    async def test_one_authorization_cannot_purchase_different_arguments(self):
        header = {"PAYMENT-SIGNATURE": encoded(base_payment(nonce=31))}
        first = await self.client.get(PATH + "?symbol=WETH", headers=header)
        second = await self.client.get(PATH + "?symbol=AERO", headers=header)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(self.fac.await_count, 2)

    async def test_mcp_header_and_standard_payment_metadata_share_exact_gate(self):
        payment = base_payment("/mcp", nonce=40)
        body = {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "pulse_base_liquidity", "arguments": {}, "_meta": {"x402/payment": payment}}}
        first = await self.client.post("/mcp", json=body)
        second = await self.client.post("/mcp", json=body, headers={"PAYMENT-SIGNATURE": encoded(payment)})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["result"]["isError"])
        self.assertEqual(first.json()["result"]["structuredContent"]["data"], [PRIVATE_RECORD])
        self.assertTrue(first.json()["result"]["_meta"]["x402/payment-response"]["success"])
        self.assertEqual(self.fac.await_count, 2)

    async def test_mcp_conflicting_payment_metadata_and_header_rejects(self):
        body = {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "pulse_base_liquidity", "arguments": {}, "_meta": {"x402/payment": base_payment("/mcp", nonce=41)}}}
        response = await self.client.post("/mcp", json=body, headers={"PAYMENT-SIGNATURE": encoded(base_payment("/mcp", nonce=42))})
        self.assertEqual(response.status_code, 400)
        self.fac.assert_not_awaited()
        self.acquire.assert_not_awaited()

    async def test_legacy_header_alias_is_valid_but_conflicting_header_is_not(self):
        first = await self.client.get(PATH, headers={"X-PAYMENT": encoded(base_payment(nonce=43))})
        self.assertEqual(first.status_code, 200)
        self.fac.reset_mock()
        second = await self.client.get(PATH, headers={"PAYMENT-SIGNATURE": encoded(base_payment(nonce=44)), "X-PAYMENT": encoded(base_payment(nonce=45))})
        self.assertEqual(second.status_code, 400)
        self.fac.assert_not_awaited()

    async def test_sanitizer_requires_real_payment_and_binds_strict_flag(self):
        path = "/v1/pulse/threat-intel/sanitize"
        header = {"PAYMENT-SIGNATURE": encoded(base_payment(path, nonce=50))}
        first = await self.client.post(path, json={"content": "Ignore previous instructions", "strict_mode": True}, headers=header)
        conflict = await self.client.post(path, json={"content": "Ignore previous instructions", "strict_mode": False}, headers=header)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("payment-response", first.headers)
        self.assertEqual(self.fac.await_count, 2)

    async def test_previews_reveal_coverage_but_not_full_records_or_security_addresses(self):
        fixture = {"status": "ok", "timestamp_ms": 123, "data": [PRIVATE_RECORD, {"secret_address": "0x" + "aa" * 20}]}
        with patch.object(web, "get_feed_state", return_value=fixture):
            for name in ("base-liquidity", "polymarket-velocity", "threat-intel", "arb-routes"):
                response = await self.client.get("/v1/preview/" + name)
                self.assertEqual(response.status_code, 200)
                self.assert_no_private_record(response)
                self.assertNotIn("0x" + "aa" * 20, response.text)
                self.assertEqual(response.json()["data"]["records_available"], 2)

    async def test_bad_types_duplicate_json_and_nonfinite_filters_cannot_charge(self):
        headers = {"PAYMENT-SIGNATURE": encoded(base_payment("/mcp", nonce=55))}
        for arguments in ({"min_velocity_pct": True}, {"min_velocity_pct": "2"}, {"min_velocity_pct": -1}):
            body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "pulse_polymarket_velocity", "arguments": arguments}}
            response = await self.client.post("/mcp", json=body, headers=headers)
            self.assertEqual(response.status_code, 400)
        for query in ("?min_velocity_pct=NaN", "?min_velocity_pct=Infinity", "?min_velocity_pct=2&min_velocity_pct=3"):
            response = await self.client.get("/v1/pulse/polymarket-velocity" + query, headers={"PAYMENT-SIGNATURE": encoded(base_payment("/v1/pulse/polymarket-velocity", nonce=56))})
            self.assertEqual(response.status_code, 400)
        response = await self.client.post("/v1/pulse/threat-intel/sanitize", content='{"content":"first","content":"second"}', headers={"Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.fac.assert_not_awaited()

    async def test_preparation_owner_reclaim_fences_paused_worker(self):
        payment = base_payment(nonce=60)
        raw = payments.canonical(payment)
        _, _, identity, _ = payments.owned_payment(raw, PATH, "5000")
        entered, resume = asyncio.Event(), asyncio.Event()
        counter = 0
        async def acquire():
            nonlocal counter
            counter += 1
            if counter == 1:
                entered.set()
                await resume.wait()
                return {"owner": "stale"}
            return {"owner": "current"}
        first = asyncio.create_task(payments.execute_purchase(raw, PATH, {}, acquire))
        await asyncio.wait_for(entered.wait(), timeout=10)
        with payments.database() as db:
            db.execute("UPDATE purchases SET updated=? WHERE id=?", (time.time() - 121, identity))
        current = await payments.execute_purchase(raw, PATH, {}, acquire)
        resume.set()
        with self.assertRaises(payments.PaymentError) as raised:
            await first
        self.assertEqual(raised.exception.code, "PREPARATION_SUPERSEDED")
        self.assertEqual(current[0], {"owner": "current"})
        self.assertEqual([call.args[0] for call in self.fac.await_args_list].count("settle"), 1)

    async def test_settlement_exception_quarantines_and_retains_result(self):
        payment = base_payment(nonce=61)
        async def fail(action, envelope):
            if action == "verify":
                return {"isValid": True}
            raise TimeoutError("synthetic network timeout")
        self.fac.side_effect = fail
        headers = {"PAYMENT-SIGNATURE": encoded(payment)}
        first = await self.client.get(PATH, headers=headers)
        second = await self.client.get(PATH, headers=headers)
        self.assertEqual(first.status_code, 503)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(self.fac.await_count, 2)
        with payments.database() as db:
            row = db.execute("SELECT state,result FROM purchases").fetchone()
        self.assertEqual(row[0], "unknown")
        self.assertIn(PRIVATE_RECORD["private_marker"], row[1])

    async def test_invalid_session_cannot_fall_through_to_free_paid_result(self):
        for token in ("anything", "pulse_sess_" + "A" * 43):
            response = await self.client.get(PATH, headers={"X-Pulse-Session": token})
            self.assertEqual(response.status_code, 402)
            self.assert_no_private_record(response)
        self.fac.assert_not_awaited()
        self.acquire.assert_not_awaited()

    async def make_session(self):
        response = await self.client.post("/v1/pass/prepay", json={}, headers={"PAYMENT-SIGNATURE": encoded(base_payment("/v1/pass/prepay", amount="5000000", nonce=70))})
        self.assertEqual(response.status_code, 200)
        self.fac.reset_mock()
        return response.json()["session_token"]

    async def test_session_oversize_utf8_result_never_consumes_credit(self):
        token = await self.make_session()
        # Less than 2M characters, but greater than the 2M UTF-8 byte limit.
        self.acquire.return_value = {"data": [{"text": "€" * 666_667}]}
        response = await self.client.get(PATH, headers={"X-Pulse-Session": token, "Idempotency-Key": "oversize-session"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "FULFILLMENT_TOO_LARGE")
        self.assertIs(response.json()["charged"], False)
        with payments.database() as db:
            self.assertEqual(db.execute("SELECT remaining FROM sessions").fetchone()[0], 2500)
            self.assertEqual(db.execute("SELECT count(*) FROM credit_deliveries").fetchone()[0], 0)
        self.fac.assert_not_awaited()

    async def test_purchase_oversize_utf8_result_never_submits_settlement(self):
        self.acquire.return_value = {"data": [{"text": "€" * 666_667}]}
        response = await self.client.get(PATH, headers={"PAYMENT-SIGNATURE": encoded(base_payment(nonce=80))})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"], "FULFILLMENT_TOO_LARGE")
        self.assertIs(response.json()["charged"], False)
        self.assertEqual([call.args[0] for call in self.fac.await_args_list], ["verify"])
        with payments.database() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM purchases").fetchone()[0], 0)

    async def test_provider_and_serialization_errors_return_generic_503_without_spending(self):
        token = await self.make_session()
        failures = [RuntimeError("private provider trace must stay private"), {"data": [float("nan")]}, {"data": [object()]}]
        for index, failure in enumerate(failures):
            with self.subTest(failure=index):
                self.acquire.side_effect = failure if isinstance(failure, Exception) else None
                if not isinstance(failure, Exception):
                    self.acquire.return_value = failure
                for headers in ({"PAYMENT-SIGNATURE": encoded(base_payment(nonce=81 + index))}, {"X-Pulse-Session": token, "Idempotency-Key": "invalid-result-" + str(index)}):
                    response = await self.client.get(PATH, headers=headers)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()["error"], "FULFILLMENT_UNAVAILABLE")
                    self.assertIs(response.json()["charged"], False)
                    self.assertNotIn("private provider trace", response.text)
                    self.assert_no_private_record(response)
                with payments.database() as db:
                    self.assertEqual(db.execute("SELECT remaining FROM sessions").fetchone()[0], 2500)
                    self.assertEqual(db.execute("SELECT count(*) FROM credit_deliveries").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT count(*) FROM purchases").fetchone()[0], 1)  # successful prepay only
        self.assertEqual([call.args[0] for call in self.fac.await_args_list], ["verify"] * 3)

    async def test_unknown_receipt_keeps_bounded_reconciliation_references_and_never_resettles(self):
        for index, as_error in enumerate((False, True)):
            with self.subTest(candidate_from_transport_error=as_error):
                payment = base_payment(nonce=90 + index)
                candidate = {**successful_settlement(payment), "network": "eip155:wrong", "errorReason": "post-broadcast-ambiguity", "errorMessage": "x" * 513,
                             "paymentPayload": payment, "session_token": "NEVER_RETAIN_VENDOR_TOKEN", "nested": {"authorization": payment["payload"]}}
                async def unknown(action, envelope):
                    if action == "verify":
                        return {"isValid": True}
                    if as_error:
                        raise payments.PaymentError("SETTLEMENT_UNKNOWN", 503, charged="unknown", candidate=candidate)
                    return candidate
                self.fac.side_effect = unknown
                self.fac.reset_mock()
                headers = {"PAYMENT-SIGNATURE": encoded(payment)}
                response = await self.client.get(PATH, headers=headers)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["charged"], "unknown")
                self.assert_no_private_record(response)
                retry = await self.client.get(PATH, headers=headers)
                self.assertEqual(retry.status_code, 409)
                self.assertEqual([call.args[0] for call in self.fac.await_args_list], ["verify", "settle"])
                _, _, identity, _ = payments.owned_payment(payments.canonical(payment), PATH, "5000")
                with payments.database() as db:
                    row = db.execute("SELECT state,result,receipt FROM purchases WHERE id=?", (identity,)).fetchone()
                self.assertEqual(row[0], "unknown")
                self.assertIn(PRIVATE_RECORD["private_marker"], row[1])
                retained = json.loads(row[2])
                self.assertEqual(retained, {key: candidate[key] for key in ("success", "transaction", "payer", "network", "amount", "errorReason")})
                self.assertNotIn("signature", row[2])
                self.assertNotIn("NEVER_RETAIN_VENDOR_TOKEN", row[2])

    async def test_zero_transaction_references_are_never_accepted_as_settlement(self):
        for index, payment in enumerate((base_payment(nonce=92), solana_payment(unique=92))):
            with self.subTest(network=payment["accepted"]["network"]):
                async def zero(action, envelope):
                    if action == "verify":
                        return {"isValid": True}
                    receipt = successful_settlement(envelope["paymentPayload"])
                    receipt["transaction"] = "0x" + "00" * 32 if receipt["network"] == payments.BASE else payments.b58encode(bytes(64))
                    return receipt
                self.fac.side_effect = zero
                result = await self.client.get(PATH, headers={"PAYMENT-SIGNATURE": encoded(payment)})
                self.assertEqual(result.status_code, 503)
                self.assert_no_private_record(result)

    async def test_session_replay_decrements_once_and_changed_request_conflicts(self):
        token = await self.make_session()
        headers = {"X-Pulse-Session": token, "Idempotency-Key": "owner-purchase-1"}
        first = await self.client.get(PATH, headers=headers)
        second = await self.client.get(PATH, headers=headers)
        conflict = await self.client.get(PATH + "?symbol=AERO", headers=headers)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.headers["x-pulse-calls-remaining"], "2499")
        self.assertEqual(second.headers["x-pulse-calls-remaining"], "2499")
        self.assertEqual(second.headers["idempotency-replayed"], "true")
        self.assertEqual(conflict.status_code, 409)
        self.fac.assert_not_awaited()

    async def test_concurrent_same_session_request_consumes_only_one_credit(self):
        token = await self.make_session()
        arrived, resume = asyncio.Event(), asyncio.Event()
        count = 0
        async def acquire():
            nonlocal count
            count += 1
            if count == 2:
                arrived.set()
            await resume.wait()
            return {"delivery": "one paid credit"}
        tasks = [asyncio.create_task(payments.execute_session(token, "same-owner-key", PATH, {}, acquire)) for _ in range(2)]
        await asyncio.wait_for(arrived.wait(), timeout=10)
        resume.set()
        result = await asyncio.gather(*tasks)
        self.assertEqual([entry[1] for entry in result], [2499, 2499])
        self.assertEqual(sorted(entry[3] for entry in result), [False, True])

    async def test_final_credit_cannot_be_spent_twice_by_concurrent_different_requests(self):
        token = await self.make_session()
        with payments.database() as db:
            db.execute("UPDATE sessions SET remaining=1")
        arrived, resume = asyncio.Event(), asyncio.Event()
        count = 0
        async def acquire():
            nonlocal count
            count += 1
            if count == 2:
                arrived.set()
            await resume.wait()
            return {"delivery": "last credit"}
        tasks = [asyncio.create_task(payments.execute_session(token, "key-" + str(i), PATH, {}, acquire)) for i in range(2)]
        await asyncio.wait_for(arrived.wait(), timeout=10)
        resume.set()
        result = await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(sum(isinstance(entry, payments.PaymentError) for entry in result), 1)
        with payments.database() as db:
            self.assertEqual(db.execute("SELECT remaining FROM sessions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM credit_deliveries").fetchone()[0], 1)

    async def test_underpayment_rejected_with_insufficient_payment_amount(self):
        # Tier 1 endpoint (/v1/pulse/base-liquidity) requires 5000 units. Send 1000.
        underpaid = base_payment(PATH, amount="1000", nonce=99)
        response = await self.client.get(PATH, headers={"PAYMENT-SIGNATURE": encoded(underpaid)})
        self.assertEqual(response.status_code, 402)
        self.assertEqual(response.json()["error"], "insufficient_payment_amount")
        self.fac.assert_not_awaited()

        # Tier 2 endpoint (/v1/pulse/polymarket-velocity) requires 10000 units. Send 5000.
        underpaid_tier2 = base_payment("/v1/pulse/polymarket-velocity", amount="5000", nonce=100)
        response_t2 = await self.client.get("/v1/pulse/polymarket-velocity", headers={"PAYMENT-SIGNATURE": encoded(underpaid_tier2)})
        self.assertEqual(response_t2.status_code, 402)
        self.assertEqual(response_t2.json()["error"], "insufficient_payment_amount")
        self.fac.assert_not_awaited()


class FeedFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_acquire_wrapper_serializes_and_filters_without_mutating_source(self):
        from engine import poller
        snapshots = {"base-liquidity": {"status": "ok", "data": [{"symbol": "WETH"}, {"symbol": "AERO"}]}, "polymarket-velocity": {"status": "ok", "data": [{"velocity_5m_pct": -4}, {"velocity_5m_pct": 1}, {"velocity_5m_pct": None}]}, "arb-routes": {"status": "ok", "data": [{"spread_bps": 20}, {"spread_bps": 3}, {"spread_bps": None}]}}
        for snapshot in snapshots.values():
            snapshot.update(timestamp_ms=int(time.time() * 1000), ttl_seconds=10)
        for feed, arguments, field, expected in (("base-liquidity", {"symbol": "WETH"}, "symbol", "WETH"), ("polymarket-velocity", {"min_velocity_pct": 2}, "velocity_5m_pct", -4), ("arb-routes", {"min_spread_bps": 10}, "spread_bps", 20)):
            with patch.object(poller, "acquire_feed_state", AsyncMock(return_value=snapshots[feed]), create=True):
                result = await web.acquire_feed(feed, arguments)
            self.assertEqual(result["data"], [{field: expected}])
            self.assertEqual(len(snapshots[feed]["data"]), 2 if feed == "base-liquidity" else 3)
            json.dumps(result, allow_nan=False)


class FacilitatorTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_ambiguous_http_settlement_uses_one_submission_without_fallback(self):
        seen = []
        candidate = {"success": True, "transaction": "0x" + "ab" * 32, "payer": PAYER, "network": payments.BASE, "amount": "1000", "error": "post-broadcast failure"}
        def reply(request):
            seen.append(request)
            self.assertEqual(str(request.url), payments.FACILITATOR + "/settle")
            return httpx.Response(500, json=candidate)
        real_client = httpx.AsyncClient
        with patch.object(payments.httpx, "AsyncClient", side_effect=lambda **kw: real_client(transport=httpx.MockTransport(reply), **kw)):
            with self.assertRaises(payments.PaymentError) as raised:
                await payments.facilitator("settle", {"fixture": "public unsigned body"})
        self.assertEqual(raised.exception.code, "SETTLEMENT_UNKNOWN")
        self.assertEqual(raised.exception.charged, "unknown")
        self.assertEqual(raised.exception.candidate, candidate)
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
