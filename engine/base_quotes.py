"""Read-only Base quotes. Discovery is not execution evidence; eth_call is.

No key loading, approvals, transaction construction, signing, or broadcasting.
"""

import asyncio
import os
import re
import time
from decimal import Decimal, InvalidOperation, localcontext
from datetime import datetime, timezone

import httpx
from eth_abi import encode, decode
from eth_utils import keccak

TOKENS = {
    "WETH": "0x4200000000000000000000000000000000000006",
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "AERO": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
}
SOLANA_TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "WSOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}
SOLANA_CHAIN = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
UNI_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNI_QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
AERO_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


class QuoteError(Exception):
    def __init__(self, code, message, http_status=503):
        super().__init__(message)
        self.code, self.http_status = code, http_status


async def _read_together(*reads):
    """Drain independent reads, retaining their original validation order."""
    tasks = [asyncio.ensure_future(read) for read in reads]
    try:
        return await asyncio.gather(*tasks, return_exceptions=True)
    except BaseException:
        # Caller cancellation must not leave HTTP requests using a closed client.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _read_value(result):
    if isinstance(result, BaseException):
        raise result
    return result


def solana_address(value):
    if not isinstance(value, str):
        raise QuoteError(
            "INVALID_TOKEN", "Solana token must be a mint address or SOL/USDC", 400
        )
    address = SOLANA_TOKENS.get(value.upper(), value)
    try:
        number = 0
        for char in address:
            number = number * 58 + B58.index(char)
        length = (
            len(number.to_bytes((number.bit_length() + 7) // 8, "big"))
            + len(address)
            - len(address.lstrip("1"))
        )
        if length != 32:
            raise ValueError()
    except (ValueError, TypeError):
        raise QuoteError(
            "INVALID_TOKEN", "Expected a valid Solana mint address or SOL/USDC", 400
        )
    return address


def token_address(value):
    if not isinstance(value, str):
        raise QuoteError(
            "INVALID_TOKEN", "Token must be a Base address or supported symbol", 400
        )
    address = TOKENS.get(value.upper(), value)
    if not ADDRESS.fullmatch(address):
        raise QuoteError(
            "INVALID_TOKEN", "Use WETH, USDC, AERO or an exact Base token address", 400
        )
    return address.lower()


def positive_decimal(value):
    try:
        if isinstance(value, bool) or value is None:
            raise InvalidOperation()
        if len(str(value)) > 128:
            raise InvalidOperation()
        number = Decimal(str(value))
        if not number.is_finite() or number <= 0 or number > Decimal("1e30"):
            raise InvalidOperation()
        return number
    except (InvalidOperation, TypeError, ValueError):
        raise QuoteError(
            "INVALID_AMOUNT",
            "amount must be a positive finite decimal at most 1e30",
            400,
        )


def validate_request(tool, args):
    if not isinstance(args, dict):
        raise QuoteError("INVALID_INPUT", "Input must be a JSON object", 400)
    chain = args.get("chain", "base")
    if chain not in ("base", "eip155:8453", 8453, "solana", SOLANA_CHAIN):
        raise QuoteError(
            "UNSUPPORTED_CHAIN", "Trading quotes support Base and Solana mainnet", 400
        )
    if tool == "arbitrage_scan":
        pair = args.get(
            "pair", "SOL/USDC" if chain in ("solana", SOLANA_CHAIN) else "WETH/USDC"
        )
        if not isinstance(pair, str) or len(pair.split("/")) != 2:
            raise QuoteError(
                "INVALID_PAIR",
                "pair must contain exactly two tokens separated by /",
                400,
            )
        tin, tout = pair.split("/")
    else:
        tin, tout = args.get(
            "tokenIn", "SOL" if chain in ("solana", SOLANA_CHAIN) else "WETH"
        ), args.get("tokenOut", "USDC")
    resolve = solana_address if chain in ("solana", SOLANA_CHAIN) else token_address
    tin, tout = resolve(tin), resolve(tout)
    if tin == tout:
        raise QuoteError("INVALID_PAIR", "Input and output tokens must differ", 400)
    amount = positive_decimal(args.get("amount", "1"))
    threshold = args.get("min_spread_bps", 0)
    if (
        isinstance(threshold, bool)
        or not isinstance(threshold, int)
        or not 0 <= threshold <= 100000
    ):
        raise QuoteError(
            "INVALID_THRESHOLD",
            "min_spread_bps must be an integer from 0 to 100000",
            400,
        )
    slippage = args.get("slippage_bps", 50)
    if (
        isinstance(slippage, bool)
        or not isinstance(slippage, int)
        or not 1 <= slippage <= 1000
    ):
        raise QuoteError(
            "INVALID_SLIPPAGE", "slippage_bps must be an integer from 1 to 1000", 400
        )
    return tin, tout, amount, threshold


class BaseQuotes:
    def __init__(self, client):
        self.client = client
        self.rpc_url = os.environ.get("BASE_RPC_URL", "https://mainnet.base.org")
        self.rpc_urls = list(
            dict.fromkeys(
                [
                    self.rpc_url,
                    "https://base-rpc.publicnode.com",
                    "https://mainnet.base.org",
                ]
            )
        )
        self.block = None
        self.pool_metadata = {}

    async def rpc(self, method, params):
        # Fixed trusted fallback hosts; environment-provided credentials are never returned.
        failures = []
        for url in self.rpc_urls:
            try:
                response = await self.client.post(
                    url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": method,
                        "params": params,
                    },
                )
                if response.status_code != 200:
                    failures.append("HTTP_ERROR")
                    continue
                payload = response.json()
                if payload.get("error"):
                    # A deterministic revert is not a transport outage.
                    if "revert" in str(payload["error"]).lower():
                        raise QuoteError(
                            "QUOTE_REVERTED", "Read-only contract quote reverted"
                        )
                    failures.append("RPC_ERROR")
                    continue
                if "result" not in payload:
                    failures.append("INVALID_RPC_RESPONSE")
                    continue
                return payload["result"]
            except QuoteError:
                raise
            except (httpx.HTTPError, ValueError, TypeError):
                failures.append("RPC_UNAVAILABLE")
        raise QuoteError(
            "RPC_UNAVAILABLE",
            "Base RPC acquisition failed on configured and public fallback providers",
        )

    async def call(self, address, signature, types=(), values=(), outputs=("uint256",)):
        data = "0x" + (keccak(text=signature)[:4] + encode(types, values)).hex()
        raw = await self.rpc("eth_call", [{"to": address, "data": data}, self.block])
        try:
            return decode(outputs, bytes.fromhex(raw.removeprefix("0x")))
        except (ValueError, TypeError, OverflowError) as exc:
            raise QuoteError(
                "INVALID_CONTRACT_RESPONSE", "Contract returned malformed quote data"
            ) from exc
        except Exception as exc:
            raise QuoteError(
                "INVALID_CONTRACT_RESPONSE",
                "Contract response could not be ABI decoded",
            ) from exc

    async def setup(self, tin, tout):
        if int(await self.rpc("eth_chainId", []), 16) != 8453:
            raise QuoteError("WRONG_RPC_CHAIN", "RPC is not Base mainnet")
        self.block = await self.rpc("eth_blockNumber", [])
        self.block_info = await self.rpc("eth_getBlockByNumber", [self.block, False])
        if not isinstance(self.block_info, dict) or not self.block_info.get("hash"):
            raise QuoteError("INVALID_BLOCK", "RPC did not return the quoted block")
        self.decimals = {}
        results = await _read_together(
            *(self.call(token, "decimals()", outputs=("uint8",)) for token in (tin, tout))
        )
        for token, result in zip((tin, tout), results):
            decimals = _read_value(result)[0]
            self.decimals[token] = decimals
        return self

    async def discover(self, tin, tout):
        pools, errors = [], []
        # Official per-token endpoint avoids symbol search ambiguity and truncation.
        try:
            response = await self.client.get(
                f"https://api.dexscreener.com/token-pairs/v1/base/{tin}"
            )
            if response.status_code != 200:
                errors.append(
                    {
                        "provider": "dexscreener",
                        "code": "HTTP_ERROR",
                        "http_status": response.status_code,
                    }
                )
            else:
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError("Expected array")
                for pool in payload:
                    actual = {
                        str((pool.get(k) or {}).get("address", "")).lower()
                        for k in ("baseToken", "quoteToken")
                    }
                    if (
                        pool.get("chainId") == "base"
                        and actual == {tin, tout}
                        and ADDRESS.fullmatch(str(pool.get("pairAddress", "")))
                    ):
                        pools.append(
                            {
                                "pool": pool["pairAddress"].lower(),
                                "dex": pool.get("dexId", "unknown"),
                                "labels": pool.get("labels") or [],
                            }
                        )
        except (httpx.HTTPError, ValueError, TypeError):
            errors.append({"provider": "dexscreener", "code": "DISCOVERY_UNAVAILABLE"})
        # On-chain Uniswap factory is a separate acquisition path, not dependent on discovery.
        for fee in (100, 500, 3000, 10000):
            try:
                pool = (
                    await self.call(
                        UNI_FACTORY,
                        "getPool(address,address,uint24)",
                        ("address", "address", "uint24"),
                        (tin, tout, fee),
                        ("address",),
                    )
                )[0]
                if int(pool, 16):
                    pools.append(
                        {
                            "pool": pool.lower(),
                            "dex": "uniswap",
                            "labels": ["v3"],
                            "fee": fee,
                        }
                    )
            except QuoteError as exc:
                errors.append(
                    {"provider": "uniswap_factory", "code": exc.code, "fee": fee}
                )
        unique = {p["pool"]: p for p in pools}
        supported = [
            p
            for p in unique.values()
            if p["dex"] in ("uniswap", "aerodrome")
            and not (
                p["dex"] == "aerodrome"
                and any(
                    "slip" in str(x).lower() or "cl" == str(x).lower()
                    for x in p.get("labels", [])
                )
            )
        ]
        return supported[:6], errors

    async def quote(self, pool, tin, tout, amount):
        address = pool["pool"]
        is_v3 = pool["dex"] == "uniswap" and (
            "v3" in pool["labels"] or "fee" in pool
        )
        fee_read = None
        if address not in self.pool_metadata:
            reads = [
                self.call(address, "token0()", outputs=("address",)),
                self.call(address, "token1()", outputs=("address",)),
            ]
            if is_v3:
                reads.append(self.call(address, "fee()", outputs=("uint24",)))
            results = await _read_together(*reads)
            actual0 = _read_value(results[0])[0].lower()
            actual1 = _read_value(results[1])[0].lower()
            self.pool_metadata[address] = {"token0": actual0, "token1": actual1}
            if is_v3:
                fee_read = results[2]
        meta = self.pool_metadata[address]
        actual0, actual1 = meta["token0"], meta["token1"]
        if {actual0, actual1} != {tin, tout}:
            raise QuoteError(
                "POOL_TOKEN_MISMATCH",
                "On-chain pool tokens do not match requested tokens",
            )
        if is_v3:
            if "fee" not in meta:
                fee = (
                    _read_value(fee_read)
                    if fee_read is not None
                    else await self.call(address, "fee()", outputs=("uint24",))
                )[0]
                factory_pool = (
                    await self.call(
                        UNI_FACTORY,
                        "getPool(address,address,uint24)",
                        ("address", "address", "uint24"),
                        (tin, tout, fee),
                        ("address",),
                    )
                )[0].lower()
                if factory_pool != address:
                    raise QuoteError(
                        "UNTRUSTED_POOL",
                        "Pool does not belong to the canonical Uniswap factory",
                    )
                meta["fee"] = fee
            fee = meta["fee"]
            result = await self.call(
                UNI_QUOTER,
                "quoteExactInputSingle((address,address,uint256,uint24,uint160))",
                ("(address,address,uint256,uint24,uint160)",),
                ((tin, tout, amount, fee, 0),),
                ("uint256", "uint160", "uint32", "uint256"),
            )
            out, sqrt_after, ticks, gas = result
            detail = {
                "method": "uniswap_v3_quoter_v2",
                "fee_ppm": fee,
                "sqrt_price_x96_after": str(sqrt_after),
                "initialized_ticks_crossed": ticks,
                "gas_units_quote_estimate": gas,
            }
        elif pool["dex"] == "aerodrome" and not any(
            "slip" in x.lower() or "cl" == x.lower() for x in pool["labels"]
        ):
            if "aero_verified" not in meta:
                registered = (
                    await self.call(
                        AERO_FACTORY,
                        "isPool(address)",
                        ("address",),
                        (address,),
                        ("bool",),
                    )
                )[0]
                if not registered:
                    raise QuoteError(
                        "UNTRUSTED_POOL",
                        "Pool does not belong to the canonical Aerodrome factory",
                    )
                stable = (await self.call(address, "stable()", outputs=("bool",)))[0]
                fee_bps = (
                    await self.call(
                        AERO_FACTORY,
                        "getFee(address,bool)",
                        ("address", "bool"),
                        (address, stable),
                    )
                )[0]
                meta.update(aero_verified=True, fee_bps=fee_bps, stable=stable)
            out = (
                await self.call(
                    address,
                    "getAmountOut(uint256,address)",
                    ("uint256", "address"),
                    (amount, tin),
                )
            )[0]
            detail = {
                "method": "aerodrome_pool_getAmountOut",
                "fee_bps": meta["fee_bps"],
                "stable": meta["stable"],
                "gas_units_quote_estimate": None,
            }
        else:
            raise QuoteError(
                "UNSUPPORTED_POOL",
                "Pool family has no reviewed read-only quote adapter",
            )
        if out <= 0:
            raise QuoteError("ZERO_OUTPUT", "Contract quote returned zero output")
        return {
            "dex": pool["dex"],
            "pool": address,
            "token_in": tin,
            "token_out": tout,
            "amount_in_raw": str(amount),
            "amount_out_raw": str(out),
            "decimals_in": self.decimals[tin],
            "decimals_out": self.decimals[tout],
            "lp_fee_included": True,
            **detail,
        }
