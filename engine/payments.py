"""One payment/credit lifecycle for HTTP, MCP and sanitizer delivery.

This module has no buyer keys and never submits a second settlement attempt.
PULSE_PAYMENTS_DB must reside on durable storage across deployments.
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from eth_account import Account
from eth_account.messages import encode_typed_data

BASE = "eip155:8453"
SOLANA = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
FEE_PAYER = "CjNFTjvBhbJJd2B5ePPMHRLx1ELZpa8dwQgGL727eKww"
FACILITATOR = "https://facilitator.payai.network"
PUBLIC_URL = os.getenv("PUBLIC_URL", "https://pulse402-oracle.onrender.com").rstrip("/")
ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class PaymentError(Exception):
    def __init__(self, code, status=402, *, charged=False, candidate=None):
        super().__init__(code)
        self.code, self.status, self.charged = code, status, charged
        self.candidate = candidate


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def b58encode(raw):
    number, result = int.from_bytes(raw, "big"), ""
    while number:
        number, digit = divmod(number, 58)
        result = B58[digit] + result
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + result


def b58decode(value):
    number = 0
    for char in value:
        number = number * 58 + B58.index(char)
    return b"\0" * (len(value) - len(value.lstrip("1"))) + number.to_bytes((number.bit_length() + 7) // 8, "big")


def requirements(amount="1000"):
    base_wallet = os.getenv("PAYOUT_WALLET_ADDRESS", "0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6").strip()
    sol_wallet = os.getenv("SOLANA_MERCHANT_ADDRESS", "9wuMysgXwNhtQeqJgGByAiMYc4EjzZ5wUSmnjrrt3ffh").strip()
    if base_wallet.lower() == "0x73279fa4bada7cac888c62cda4f5c8104765f6f1":
        base_wallet = "0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6"
    if sol_wallet == "3tQ4GthjUMqNsyrb1rZMocMHoNewFQWjDUvHZqWeYuCN":
        sol_wallet = "9wuMysgXwNhtQeqJgGByAiMYc4EjzZ5wUSmnjrrt3ffh"
    try:
        configured = ADDRESS.fullmatch(base_wallet) and len(b58decode(sol_wallet)) == 32
    except (ValueError, TypeError):
        configured = False
    if not configured:
        raise PaymentError("INVALID_MERCHANT_CONFIGURATION", 503)
    return [
        {"scheme": "exact", "network": BASE, "asset": USDC, "amount": str(amount), "maxTimeoutSeconds": 300, "payTo": base_wallet, "extra": {"name": "USD Coin", "version": "2"}},
        {"scheme": "exact", "network": SOLANA, "asset": SOL_USDC, "amount": str(amount), "maxTimeoutSeconds": 300, "payTo": sol_wallet, "extra": {"name": "USD Coin (Solana)", "version": "2", "feePayer": FEE_PAYER}},
    ]


def challenge(path, amount="1000", description="Pulse402 paid oracle delivery"):
    return {"x402Version": 2, "error": "Payment required", "resource": {"url": PUBLIC_URL + path, "description": description, "mimeType": "application/json"}, "accepts": requirements(amount)}


def payment_header(headers, metadata=None):
    values = [headers.get(name) for name in ("PAYMENT-SIGNATURE", "X-PAYMENT", "X-Payment-Signature") if headers.get(name)]
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise PaymentError("INVALID_PAYMENT_METADATA", 400)
        payment = metadata.get("x402/payment")
        if payment is not None:
            values.append(base64.b64encode(canonical(payment).encode()).decode())
    decoded = []
    for value in values:
        if not isinstance(value, str) or not 1 <= len(value) <= 32768:
            raise PaymentError("INVALID_PAYMENT_HEADER", 400)
        try:
            decoded.append(json.loads(value if value.lstrip().startswith("{") else base64.b64decode(value, validate=True)))
        except Exception:
            raise PaymentError("INVALID_PAYMENT_HEADER", 402)
    if decoded and any(item != decoded[0] for item in decoded):
        raise PaymentError("CONFLICTING_PAYMENT_HEADERS", 400)
    try:
        return canonical(decoded[0]) if decoded else None
    except (ValueError, TypeError):
        raise PaymentError("INVALID_PAYMENT_HEADER", 402)


def owned_payment(raw_header, path, amount):
    """Authenticate the buyer locally before returning any retained paid output."""
    try:
        payment = json.loads(raw_header)
        if not isinstance(payment, dict) or type(payment.get("x402Version")) is not int or payment["x402Version"] != 2:
            raise ValueError()
        accepted = payment["accepted"]
        quote = next(item for item in requirements(amount) if item["network"] == accepted.get("network"))
        if accepted != quote or payment.get("resource", {}).get("url") != PUBLIC_URL + path:
            raise ValueError()
        inner = payment["payload"]
        if quote["network"] == BASE:
            auth, signature = inner["authorization"], inner["signature"]
            if not ADDRESS.fullmatch(str(auth.get("from", ""))) or str(auth["to"]).lower() != quote["payTo"].lower() or str(auth["value"]) != str(amount):
                raise ValueError()
            if not re.fullmatch(r"0x[0-9a-fA-F]{64}", auth["nonce"]) or not re.fullmatch(r"0x[0-9a-fA-F]{130}", signature):
                raise ValueError()
            fields = [("from", "address"), ("to", "address"), ("value", "uint256"), ("validAfter", "uint256"), ("validBefore", "uint256"), ("nonce", "bytes32")]
            message = {key: auth[key] if kind in ("address", "bytes32") else int(auth[key]) for key, kind in fields}
            typed = {"types": {"EIP712Domain": [{"name": n, "type": t} for n, t in [("name", "string"), ("version", "string"), ("chainId", "uint256"), ("verifyingContract", "address")]], "TransferWithAuthorization": [{"name": n, "type": t} for n, t in fields]}, "primaryType": "TransferWithAuthorization", "domain": {"name": "USD Coin", "version": "2", "chainId": 8453, "verifyingContract": USDC}, "message": message}
            payer = Account.recover_message(encode_typed_data(full_message=typed), signature=signature)
            if payer.lower() != auth["from"].lower():
                raise ValueError()
            identity = digest({"network": BASE, "payer": payer.lower(), "nonce": auth["nonce"].lower()})
        else:
            wire = base64.b64decode(inner["transaction"], validate=True)
            # Exact two-signature sponsored v0 structure; facilitator also validates
            # all program instructions, mint, recipient and raw transfer amount.
            if len(wire) > 1232 or wire[0] != 2:
                raise ValueError()
            message = wire[129:]
            if len(message) < 101 or message[0] != 128 or message[1] != 2 or message[4] & 128 or message[4] < 2:
                raise ValueError()
            if message[5:37] != b58decode(FEE_PAYER):
                raise ValueError()
            Ed25519PublicKey.from_public_bytes(message[37:69]).verify(wire[65:129], message)
            payer = b58encode(message[37:69])
            identity = digest({"network": SOLANA, "message": message.hex()})
        return payment, quote, identity, payer
    except PaymentError:
        raise
    except Exception:
        raise PaymentError("INVALID_PAYMENT_AUTHORIZATION")


@contextmanager
def database():
    path = Path(os.getenv("PULSE_PAYMENTS_DB", "state/payments.sqlite3"))
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS purchases(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, state TEXT NOT NULL, owner TEXT, updated REAL NOT NULL, result TEXT, receipt TEXT);
      CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, expires REAL NOT NULL, remaining INTEGER NOT NULL CHECK(remaining>=0), purchase_id TEXT NOT NULL UNIQUE);
      CREATE TABLE IF NOT EXISTS credit_deliveries(session_hash TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, result TEXT NOT NULL, PRIMARY KEY(session_hash,request_id));
    """)
    try:
        yield db
    finally:
        db.close()


async def facilitator(action, envelope):
    # One HTTP submission only. A settlement error is never followed by fallback.
    value = None
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            response = await client.post(FACILITATOR + "/" + action, json=envelope)
            value = response.json()
            if response.status_code != 200 or not isinstance(value, dict):
                raise ValueError()
            return value
    except Exception:
        raise PaymentError("PAYMENT_VERIFICATION_UNAVAILABLE" if action == "verify" else "SETTLEMENT_UNKNOWN", 503, charged="unknown" if action == "settle" else False, candidate=value if action == "settle" else None)


def receipt_for(value, quote, payer, path):
    tx = value.get("transaction")
    pattern = r"0x[0-9a-fA-F]{64}" if quote["network"] == BASE else r"[1-9A-HJ-NP-Za-km-z]{80,90}"
    actual_payer = value.get("payer")
    if value.get("success") is not True or not isinstance(tx, str) or not re.fullmatch(pattern, tx) or value.get("network") != quote["network"] or not isinstance(actual_payer, str):
        raise ValueError()
    if quote["network"] == BASE and int(tx, 16) == 0:
        raise ValueError()
    if (actual_payer.lower() != payer.lower() if quote["network"] == BASE else actual_payer != payer):
        raise ValueError()
    if quote["network"] == SOLANA and (len(b58decode(tx)) != 64 or not any(b58decode(tx))):
        raise ValueError()
    if "amount" in value and value["amount"] != quote["amount"]:
        raise ValueError()
    resource = value.get("resource", PUBLIC_URL + path)
    if isinstance(resource, dict):
        resource = resource.get("url")
    if resource != PUBLIC_URL + path:
        raise ValueError()
    return {"success": True, "payer": payer, "transaction": tx, "amount": quote["amount"], "network": quote["network"], "resource": {"url": PUBLIC_URL + path}}


def _new_window(payment, network):
    if network == BASE:
        auth, now = payment["payload"]["authorization"], int(time.time())
        if not 0 <= int(auth["validAfter"]) <= now or not now < int(auth["validBefore"]) <= now + 300:
            raise PaymentError("INVALID_PAYMENT_WINDOW", 402)


async def execute_purchase(raw_header, path, arguments, acquire, *, amount="1000", prepay=False):
    payment, quote, identity, payer = owned_payment(raw_header, path, amount)
    fingerprint, owner, now = digest({"path": path, "arguments": arguments}), secrets.token_hex(16), time.time()
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM purchases WHERE id=?", (identity,)).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise PaymentError("PAYMENT_REQUEST_CONFLICT", 409)
            if previous["state"] == "delivered":
                return json.loads(previous["result"]), json.loads(previous["receipt"]), True
            if previous["state"] != "preparing" or previous["updated"] > now - 120:
                raise PaymentError("PAYMENT_RECOVERY_REQUIRED", 409, charged="unknown" if previous["state"] != "preparing" else False)
            db.execute("UPDATE purchases SET owner=?,updated=? WHERE id=? AND state='preparing'", (owner, now, identity))
        else:
            db.execute("INSERT INTO purchases(id,fingerprint,state,owner,updated) VALUES(?,?,'preparing',?,?)", (identity, fingerprint, owner, now))
        db.commit()
    envelope = {"x402Version": 2, "paymentPayload": payment, "paymentRequirements": quote}
    try:
        _new_window(payment, quote["network"])
        verified = await facilitator("verify", envelope)
        if verified.get("isValid") is not True or (verified.get("payer") and (str(verified["payer"]).lower() != payer.lower() if quote["network"] == BASE else verified["payer"] != payer)):
            raise PaymentError("PAYMENT_NOT_VERIFIED")
        try:
            result = await acquire()
            encoded = canonical(result)
        except PaymentError:
            raise
        except Exception:
            raise PaymentError("FULFILLMENT_UNAVAILABLE", 503)
        if len(encoded.encode()) > 2_000_000:
            raise PaymentError("FULFILLMENT_TOO_LARGE", 503)
        _new_window(payment, quote["network"])
        with database() as db:
            changed = db.execute("UPDATE purchases SET state='settling',result=?,updated=? WHERE id=? AND owner=? AND state='preparing'", (encoded, time.time(), identity, owner)).rowcount
            if changed != 1:
                raise PaymentError("PREPARATION_SUPERSEDED", 409)
    except BaseException:
        with database() as db:
            db.execute("DELETE FROM purchases WHERE id=? AND owner=? AND state='preparing'", (identity, owner))
        raise
    settled = None
    try:
        settled = await facilitator("settle", envelope)
        receipt = receipt_for(settled, quote, payer, path)
    except BaseException as error:
        candidate = settled if isinstance(settled, dict) else getattr(error, "candidate", None)
        # Keep reconciliation references, without storing a raw signed payment
        # echoed inside a vendor error document.
        candidate = {key: value for key, value in (candidate or {}).items()
                     if key in ("success", "transaction", "payer", "network", "amount", "errorReason", "errorMessage")
                     and isinstance(value, (str, bool, int)) and len(str(value)) <= 512} if isinstance(candidate, dict) else {}
        with database() as db:
            db.execute("UPDATE purchases SET state='unknown',receipt=?,updated=? WHERE id=? AND owner=? AND state='settling'", (canonical(candidate), time.time(), identity, owner))
        raise PaymentError("SETTLEMENT_UNKNOWN", 503, charged="unknown")
    try:
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            if prepay:
                db.execute("INSERT INTO sessions(token_hash,expires,remaining,purchase_id) VALUES(?,?,6000,?)", (hashlib.sha256(result["session_token"].encode()).hexdigest(), result["expires_at_unix"], identity))
            changed = db.execute("UPDATE purchases SET state='delivered',receipt=?,updated=? WHERE id=? AND owner=? AND state='settling'", (canonical(receipt), time.time(), identity, owner)).rowcount
            if changed != 1:
                raise PaymentError("PAYMENT_RECOVERY_REQUIRED", 503, charged="unknown")
            db.commit()
    except Exception:
        # The durable settling row retains the prepared result. A confirmed
        # transfer followed by a storage failure must never look safely unpaid.
        raise PaymentError("PAYMENT_RECOVERY_REQUIRED", 503, charged="unknown")
    # Log only confirmed settlement references, never authorizations/session tokens.
    import logging
    logging.getLogger("pulse402.payments").info("settlement_confirmed operation=%s resource=%s network=%s amount=%s transaction=%s", identity, path, quote["network"], amount, receipt["transaction"])
    return result, receipt, False


async def execute_session(token, request_id, path, arguments, acquire):
    if not isinstance(token, str) or not re.fullmatch(r"pulse_sess_[A-Za-z0-9_-]{43}", token):
        raise PaymentError("INVALID_SESSION")
    if request_id is None:
        request_id = secrets.token_hex(16)
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
        raise PaymentError("INVALID_IDEMPOTENCY_KEY", 400)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    fingerprint = digest({"path": path, "arguments": arguments})
    with database() as db:
        session = db.execute("SELECT * FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
        if not session or session["expires"] <= time.time():
            raise PaymentError("INVALID_OR_EXPIRED_SESSION")
        previous = db.execute("SELECT * FROM credit_deliveries WHERE session_hash=? AND request_id=?", (token_hash, request_id)).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise PaymentError("SESSION_REQUEST_CONFLICT", 409)
            return json.loads(previous["result"]), session["remaining"], request_id, True
        if session["remaining"] <= 0:
            raise PaymentError("SESSION_EXHAUSTED")
    try:
        result = await acquire()
        encoded = canonical(result)
    except PaymentError:
        raise
    except Exception:
        raise PaymentError("FULFILLMENT_UNAVAILABLE", 503)
    if len(encoded.encode()) > 2_000_000:
        raise PaymentError("FULFILLMENT_TOO_LARGE", 503)
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT * FROM credit_deliveries WHERE session_hash=? AND request_id=?", (token_hash, request_id)).fetchone()
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise PaymentError("SESSION_REQUEST_CONFLICT", 409)
            session = db.execute("SELECT remaining FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()
            return json.loads(previous["result"]), session["remaining"], request_id, True
        changed = db.execute("UPDATE sessions SET remaining=remaining-1 WHERE token_hash=? AND remaining>0 AND expires>?", (token_hash, time.time())).rowcount
        if changed != 1:
            raise PaymentError("SESSION_EXHAUSTED_OR_EXPIRED")
        db.execute("INSERT INTO credit_deliveries VALUES(?,?,?,?)", (token_hash, request_id, fingerprint, encoded))
        remaining = db.execute("SELECT remaining FROM sessions WHERE token_hash=?", (token_hash,)).fetchone()[0]
        db.commit()
    return result, remaining, request_id, False


def prepay_result():
    return {"status": "active", "session_token": "pulse_sess_" + secrets.token_urlsafe(32), "calls_allocated": 6000, "calls_remaining": 6000, "expires_at_unix": int(time.time()) + 7 * 86400, "instructions": "Attach X-Pulse-Session and a unique Idempotency-Key per call. Reuse the key and identical arguments to recover a lost response without another credit."}
