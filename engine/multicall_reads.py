"""Reviewed caller-independent Base reads only; no transaction or signing API.

Deployment/source: https://github.com/mds1/multicall3 . The official deployment
runtime and Base eth_getCode matched on 2026-10-28 (3808 bytes). aggregate3 uses
CALL: unsupported selectors/targets must retain isolated eth_call semantics.
"""
import re

from eth_abi import decode, encode
from eth_utils import keccak

from .base_quotes import ADDRESS, UNI_FACTORY, UNI_QUOTER, QuoteError

ADDRESS_MULTICALL = "0xcA11bde05977b3631167028862bE2a173976CA11"
CODE_HASH = "0xd5c15df687b16f2ff992fc8d767b4216323184a2bbc6ee2f9c398c318e770891"
SELECTOR = keccak(text="aggregate3((address,bool,bytes)[])")[:4]
MAX_CALLS = 40
MAX_QUOTE_CALLS = 4
GAS_CEILING = 20_000_000

SPECS = {
    keccak(text=signature)[:4]: (types, role)
    for signature, types, role in (
        ("decimals()", (), "token"),
        ("balanceOf(address)", ("address",), "token"),
        ("getPool(address,address,uint24)", ("address", "address", "uint24"), "factory"),
        ("quoteExactInputSingle((address,address,uint256,uint24,uint160))", ("(address,address,uint256,uint24,uint160)",), "quoter"),
        ("token0()", (), "pool"),
        ("token1()", (), "pool"),
        ("fee()", (), "pool"),
    )
}


def _parts(params, block):
    if not isinstance(params, list) or len(params) != 2 or params[1] != block or not isinstance(block, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", block):
        raise ValueError("Read is not pinned to this block")
    transaction = params[0]
    if not isinstance(transaction, dict) or set(transaction) != {"to", "data"}:
        raise ValueError("Only destination and calldata may be aggregated")
    target, data = transaction["to"], transaction["data"]
    if not isinstance(target, str) or not ADDRESS.fullmatch(target) or not isinstance(data, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2}){4,1024}", data):
        raise ValueError("Malformed destination or calldata")
    return target.lower(), bytes.fromhex(data[2:])


def reviewed(params, block, tokens, canonical_pools):
    try:
        target, data = _parts(params, block)
        types, role = SPECS[data[:4]]
        arguments = decode(types, data[4:])
        if encode(types, arguments) != data[4:]:
            return False
        if role == "token":
            return target in tokens
        if role == "pool":
            return target in canonical_pools
        if role == "factory":
            first, second, fee = arguments
            return target == UNI_FACTORY.lower() and first.lower() in tokens and second.lower() in tokens and first.lower() != second.lower() and fee in (100, 500, 3000, 10000)
        first, second, amount, fee, sqrt_limit = arguments[0]
        return target == UNI_QUOTER.lower() and first.lower() in tokens and second.lower() in tokens and first.lower() != second.lower() and amount > 0 and fee in (100, 500, 3000, 10000) and sqrt_limit == 0
    except (ValueError, TypeError, KeyError, OverflowError):
        return False
    except Exception:
        # A malformed ABI value may raise an eth_abi-specific decoding exception.
        return False


def request(rows, block):
    if not 1 <= len(rows) <= MAX_CALLS:
        raise ValueError("Aggregate must contain one bounded chunk")
    calls = []
    for row in rows:
        if row.get("method") != "eth_call":
            raise ValueError("Only eth_call can be aggregated")
        target, data = _parts(row["params"], block)
        calls.append((target, True, data))
    data = SELECTOR + encode(("(address,bool,bytes)[]",), (calls,))
    return {"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": ADDRESS_MULTICALL, "data": "0x" + data.hex(), "gas": hex(GAS_CEILING)}, block]}


def results(value, rows):
    if not isinstance(value, dict) or value.get("jsonrpc") != "2.0" or type(value.get("id")) is not int or value["id"] != 1 or "error" in value:
        raise QuoteError("INVALID_MULTICALL_RESPONSE", "Aggregate response envelope differs from request")
    raw = value.get("result") if isinstance(value, dict) else None
    if not isinstance(raw, str) or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2}){1,262144}", raw):
        raise QuoteError("INVALID_MULTICALL_RESPONSE", "Aggregate returned no bounded result")
    try:
        encoded = bytes.fromhex(raw[2:])
        values = decode(("(bool,bytes)[]",), encoded)[0]
        if len(values) != len(rows) or encode(("(bool,bytes)[]",), (values,)) != encoded:
            raise ValueError("Aggregate result count or ABI differs")
        output = []
        for row, (success, data) in zip(rows, values):
            if len(data) > 8192:
                raise ValueError("Individual result too large")
            result = {"jsonrpc": "2.0", "id": row["id"]}
            result.update({"result": "0x" + data.hex()} if success else {"error": {"code": 3 if data else -32001, "message": "Reviewed read reverted in aggregate" if data else "Aggregate read execution budget unknown"}})
            output.append(result)
        return output
    except Exception as error:
        raise QuoteError("INVALID_MULTICALL_RESPONSE", "Aggregate result is malformed or incomplete") from error


def code_matches(value):
    return isinstance(value, str) and bool(re.fullmatch(r"0x[0-9a-fA-F]{7616}", value)) and "0x" + keccak(bytes.fromhex(value[2:])).hex() == CODE_HASH
