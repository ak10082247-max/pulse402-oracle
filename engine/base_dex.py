"""Shared Base market discovery and pinned, unsigned, exact-amount eth_call quotes."""

import asyncio
import copy
import time
import math
from collections import deque
from decimal import Decimal

import httpx

from .base_quotes import BaseQuotes, QuoteError, ADDRESS, UNI_FACTORY, UNI_QUOTER
from . import multicall_reads

TOKENS = {
    "WETH": "0x4200000000000000000000000000000000000006",
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "AERO": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
    "DEGEN": "0x4ed4E862860beD51a9570b96d89aF5E1B0Efefed",
    "VIRTUAL": "0x0b3e328455c4059EEb9e3f84b5543F74E24e7E1b",
}
TOKENS = {key: value.lower() for key, value in TOKENS.items()}
_CYCLE = None
_CYCLE_TASKS = {}
_CLIENT_CYCLES = {}
_BALANCE_HISTORY = {}
MAX_QUOTE_AGE_MS = 10_000
MAX_BLOCK_AGE_MS = 20_000


def _number(value):
    try:
        number = Decimal(str(value))
        result = float(number)
        return (
            result
            if number.is_finite() and number >= 0 and math.isfinite(result)
            else None
        )
    except Exception:
        return None


async def market_snapshot(client):
    """One discovery request per token per cycle, shared by liquidity and routing."""

    async def fetch_token(symbol, token):
        try:
            response = await client.get(
                f"https://api.dexscreener.com/token-pairs/v1/base/{token}", timeout=6
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("Expected pool list")
            return symbol, payload, None
        except (httpx.HTTPError, ValueError, TypeError):
            return symbol, [], {"symbol": symbol, "error": "DISCOVERY_UNAVAILABLE"}

    acquired = await asyncio.gather(
        *(
            fetch_token(symbol, token)
            for symbol, token in TOKENS.items()
            if symbol != "USDC"
        )
    )
    pools, errors = {}, []
    for symbol, payload, error in acquired:
        if error:
            errors.append(error)
        for pool in payload:
            if (
                not isinstance(pool, dict)
                or pool.get("chainId") != "base"
                or not ADDRESS.fullmatch(str(pool.get("pairAddress", "")))
            ):
                continue
            addresses = [
                str((pool.get(key) or {}).get("address", "")).lower()
                for key in ("baseToken", "quoteToken")
            ]
            if (
                len(set(addresses)) != 2
                or not all(ADDRESS.fullmatch(address) for address in addresses)
                or TOKENS[symbol] not in addresses
            ):
                continue
            pools[pool["pairAddress"].lower()] = pool
    return {
        "timestamp_ms": int(time.time() * 1000),
        "pools": list(pools.values()),
        "errors": errors,
    }


class CycleQuotes(BaseQuotes):
    def __init__(self, client, snapshot):
        super().__init__(client)
        self.snapshot = snapshot
        self.calls = {}
        self.discovery = {}
        self.route_quotes = {}
        self.budget = asyncio.Semaphore(6)
        self.selected_provider = None
        self.provider_errors = []
        self.providers_used = set()
        self._rpc_queue = []
        self._rpc_batch_task = None
        self._aggregate_queue = []
        self._aggregate_batch_task = None
        self.canonical_pools = set()
        self._multicall_verified = {}
        self._multicall_verification_lock = asyncio.Lock()
        self.read_counts = {"aggregated_inner_reads": 0, "aggregate_rpc_items": 0, "isolated_read_items": 0, "aggregate_fallbacks": 0, "aggregate_budget_unknown_reads": 0}

    async def _verify_multicall(self, url):
        async with self._multicall_verification_lock:
            if url in self._multicall_verified:
                return self._multicall_verified[url]
            try:
                response = await self.client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getCode", "params": [multicall_reads.ADDRESS_MULTICALL, self.block]}, timeout=5)
                payload = response.json()
                valid = response.status_code == 200 and isinstance(payload, dict) and payload.get("jsonrpc") == "2.0" and type(payload.get("id")) is int and payload["id"] == 1 and "error" not in payload and multicall_reads.code_matches(payload.get("result"))
            except (httpx.HTTPError, ValueError, TypeError):
                # This engine owns one pinned cycle. Broken code acquisition
                # must not make every queued group repeat the same timeout.
                valid = False
            self._multicall_verified[url] = valid
            if not valid and len(self.provider_errors) < 30:
                self.provider_errors.append({"method": "eth_getCode", "reason": "MULTICALL_CODE_NOT_VERIFIED", "block": self.block})
            return valid

    async def close_pending(self):
        """Drain shielded read tasks before their owning HTTP client closes."""
        pending = set(self.calls.values()) | set(self.discovery.values()) | set(self.route_quotes.values())
        if self._rpc_batch_task is not None:
            pending.add(self._rpc_batch_task)
        if self._aggregate_batch_task is not None:
            pending.add(self._aggregate_batch_task)
        pending.discard(asyncio.current_task())
        for task in pending:
            if not task.done():
                task.cancel()
        for _method, _params, future in self._rpc_queue:
            if not future.done():
                future.cancel()
        self._rpc_queue.clear()
        for _payload, future, _deadline in self._aggregate_queue:
            if not future.done():
                future.cancel()
        self._aggregate_queue.clear()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    def _retryable_rpc_error(value):
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            error = row.get("error") if isinstance(row, dict) else None
            if not isinstance(error, dict):
                continue
            message = str(error.get("message", "")).lower()
            # A contract revert is deterministic at the pinned block, not a
            # provider outage. Never repeat it merely because it is an RPC error.
            if "revert" in message:
                continue
            if error.get("code") in (-32005, -32603) or any(marker in message for marker in ("rate limit", "too many requests", "temporarily unavailable", "node is unhealthy", "server busy", "timeout")):
                return True
        return False

    async def _select(self, url):
        if self.selected_provider == url:
            return True
        response = await self.client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []},
            timeout=5,
        )
        payload = response.json()
        if (
            response.status_code != 200
            or not isinstance(payload, dict)
            or payload.get("jsonrpc") != "2.0"
            or type(payload.get("id")) is not int
            or payload["id"] != 1
            or "error" in payload
            or payload.get("result") != "0x2105"
        ):
            return False
        pinned = getattr(self, "block_info", None)
        if pinned:
            response = await self.client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_getBlockByNumber",
                    "params": [self.block, False],
                },
                timeout=5,
            )
            payload = response.json() if response.status_code == 200 else None
            observed = payload.get("result") if isinstance(payload, dict) and payload.get("jsonrpc") == "2.0" and type(payload.get("id")) is int and payload["id"] == 1 and "error" not in payload else None
            if not isinstance(observed, dict) or observed.get("hash") != pinned.get(
                "hash"
            ):
                return False
        self.selected_provider = url
        return True

    async def _post_rpc(self, payload, *, deadline=None):
        if not multicall_reads.is_aggregate(payload):
            return await self._post_rpc_transport(payload)
        future = asyncio.get_running_loop().create_future()
        self._aggregate_queue.append((payload, future, deadline))
        if self._aggregate_batch_task is None:
            self._aggregate_batch_task = asyncio.create_task(self._flush_aggregates())
        return await future

    async def _flush_aggregates(self):
        active = []
        try:
            # Let the already-bounded concurrent groups share one HTTP request.
            await asyncio.sleep(0.001)
            while self._aggregate_queue:
                pending, self._aggregate_queue = self._aggregate_queue[:multicall_reads.MAX_OUTER_BATCH], self._aggregate_queue[multicall_reads.MAX_OUTER_BATCH:]
                active = [(payload, future, deadline) for payload, future, deadline in pending if not future.done()]
                if not active:
                    continue
                requests = [{**payload, "id": index} for index, (payload, _future, _deadline) in enumerate(active, 1)]
                try:
                    deadlines = [deadline for _payload, _future, deadline in active if deadline is not None]
                    async with asyncio.timeout_at(min(deadlines) if deadlines else None):
                        if len(requests) == 1:
                            replies = [await self._post_rpc_transport(requests[0])]
                        else:
                            replies = multicall_reads.batch_results(await self._post_rpc_transport(requests), len(requests))
                    for (_payload, future, _deadline), reply in zip(active, replies):
                        if not future.done():
                            future.set_result(reply)
                except Exception as error:
                    for _payload, future, _deadline in active:
                        if not future.done():
                            future.set_exception(error)
                active = []
        finally:
            for _payload, future, _deadline in active:
                if not future.done():
                    future.cancel()
            self._aggregate_batch_task = None

    async def _post_rpc_transport(self, payload):
        rows = payload if isinstance(payload, list) else [payload]
        aggregate_batch = isinstance(payload, list) and all(multicall_reads.is_aggregate(row) for row in rows)
        needs_multicall = any(multicall_reads.is_aggregate(row) for row in rows)
        providers = list(
            dict.fromkeys(
                [
                    self.selected_provider,
                    "https://base-rpc.publicnode.com",
                    "https://mainnet.base.org",
                ]
            )
        )
        for url in providers:
            if not url:
                continue
            try:
                if not await self._select(url):
                    continue
                if needs_multicall and not await self._verify_multicall(url):
                    continue
                response = await self.client.post(url, json=payload, timeout=7)
                if response.status_code == 200:
                    value = response.json()
                    # A partial batch failure must not repeat successful sibling
                    # simulations. Per-item errors recover through their own
                    # bounded original-read fallback, after strict ID mapping.
                    if self._retryable_rpc_error(value) and not (aggregate_batch and isinstance(value, list)):
                        if len(self.provider_errors) < 30:
                            self.provider_errors.append({"method": "batch" if isinstance(payload, list) else payload["method"], "reason": "RPC_TRANSIENT_ERROR", "block": self.block})
                        continue
                    self.providers_used.add(url)
                    return value
                if len(self.provider_errors) < 30:
                    self.provider_errors.append(
                        {
                            "method": (
                                "batch"
                                if isinstance(payload, list)
                                else payload["method"]
                            ),
                            "http_status": response.status_code,
                            "block": self.block,
                        }
                    )
            except (httpx.HTTPError, ValueError, TypeError):
                continue
        raise QuoteError(
            "RPC_UNAVAILABLE",
            "No chain- and block-verified provider completed the read",
        )

    async def _read_chunk(self, rows):
        approved = [row for row in rows if multicall_reads.reviewed(row["params"], self.block, set(TOKENS.values()), self.canonical_pools)]
        isolated = [row for row in rows if row not in approved]

        async def isolated_read(group):
            self.read_counts["isolated_read_items"] += len(group)
            result = await self._post_rpc(group)
            if not isinstance(result, list) or len(result) != len(group) or any(not multicall_reads.valid_response(row) for row in result) or {row["id"] for row in result} != {row["id"] for row in group}:
                raise QuoteError("INVALID_RPC_BATCH", "Isolated read IDs do not match requests")
            return result

        # Aggregate acquisition and its one isolated fallback share a deadline.
        # A fallback never grants a new freshness window or outlives quote age.
        timeout = 7.0
        if hasattr(self, "observed_at_ms"):
            timeout = max(0.0, min(timeout, (self.observed_at_ms + MAX_QUOTE_AGE_MS) / 1000 - time.time()))
        deadline = asyncio.get_running_loop().time() + timeout

        async def acquire(group, aggregate):
            combined = None
            try:
                if timeout <= 0:
                    raise QuoteError("STALE_QUOTE", "Original quote acquisition has expired")
                async with asyncio.timeout(timeout), self.budget:
                    if aggregate:
                        self.read_counts["aggregated_inner_reads"] += len(group)
                        self.read_counts["aggregate_rpc_items"] += 1
                        try:
                            combined = multicall_reads.results(await self._post_rpc(multicall_reads.request(group, self.block), deadline=deadline), group)
                        except (QuoteError, httpx.HTTPError, ValueError, TypeError):
                            # Only whole-request failure retries the original reads.
                            # A valid individual revert was already decoded above.
                            self.read_counts["aggregate_fallbacks"] += 1
                        else:
                            # Empty failure data cannot distinguish a contract
                            # failure from EIP-150 subcall gas starvation. Repeat
                            # only those uncertain reads once in isolation.
                            unknown = {row["id"] for row in combined if row.get("error", {}).get("code") == -32001}
                            if unknown:
                                self.read_counts["aggregate_budget_unknown_reads"] += len(unknown)
                                retried = await isolated_read([row for row in group if row["id"] in unknown])
                                replacements = {row["id"]: row for row in retried}
                                combined = [replacements.get(row["id"], row) for row in combined]
                            return combined
                    return await isolated_read(group)
            except Exception:
                if combined is not None:
                    # An uncertain sibling's isolated attempt must not erase
                    # already-decoded successes or reasoned contract reverts.
                    return [row if row.get("error", {}).get("code") != -32001 else {"jsonrpc": "2.0", "id": row["id"], "error": {"code": -32000, "message": "Isolated read unavailable after aggregate budget uncertainty"}} for row in combined]
                return [{"jsonrpc": "2.0", "id": row["id"], "error": {"code": -32000, "message": "Read group unavailable"}} for row in group]

        # QuoterV2 may traverse many initialized ticks (millions of gas per
        # simulated quote). Keep its chunks smaller without reducing route or
        # exact-size coverage. Cheap reviewed metadata retains 40-read chunks.
        quotes = [row for row in approved if row["params"][0]["to"].lower() == UNI_QUOTER.lower()]
        metadata = [row for row in approved if row not in quotes]
        groups = [(metadata, True)] + [(quotes[index:index + multicall_reads.MAX_QUOTE_CALLS], True) for index in range(0, len(quotes), multicall_reads.MAX_QUOTE_CALLS)] + [(isolated, False)]
        acquired = await asyncio.gather(*(acquire(group, aggregate) for group, aggregate in groups if group))
        return [row for group in acquired for row in group]

    async def _flush_rpc(self):
        pending = []
        try:
            await asyncio.sleep(0.003)
            while self._rpc_queue:
                pending, self._rpc_queue = self._rpc_queue[:40], self._rpc_queue[40:]
                payload = [
                    {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
                    for index, (method, params, _future) in enumerate(pending, 1)
                ]
                try:
                    response = await self._read_chunk(payload)
                    if not isinstance(response, list) or len(response) != len(payload):
                        raise QuoteError("INVALID_RPC_BATCH", "Incomplete RPC batch")
                    mapped = {
                        row.get("id"): row for row in response if isinstance(row, dict)
                    }
                    if set(mapped) != set(range(1, len(payload) + 1)) or any(
                        row.get("jsonrpc") != "2.0" for row in mapped.values()
                    ):
                        raise QuoteError(
                            "INVALID_RPC_BATCH", "RPC IDs do not match requests"
                        )
                    for index, (_method, _params, future) in enumerate(pending, 1):
                        row = mapped[index]
                        if future.done():
                            continue
                        if row.get("error") or "result" not in row:
                            future.set_exception(
                                QuoteError(
                                    "RPC_READ_FAILED",
                                    "Contract read reverted or was unavailable",
                                )
                            )
                        else:
                            future.set_result(row["result"])
                except Exception as error:
                    for _method, _params, future in pending:
                        if not future.done():
                            future.set_exception(error)
        finally:
            for _method, _params, future in pending:
                if not future.done():
                    future.cancel()
            self._rpc_batch_task = None

    async def rpc(self, method, params):
        if method == "eth_call":
            future = asyncio.get_running_loop().create_future()
            self._rpc_queue.append((method, params, future))
            if self._rpc_batch_task is None:
                self._rpc_batch_task = asyncio.create_task(self._flush_rpc())
            return await asyncio.shield(future)
        payload = await self._post_rpc(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        )
        if (
            not isinstance(payload, dict)
            or payload.get("jsonrpc") != "2.0"
            or type(payload.get("id")) is not int
            or payload["id"] != 1
            or "error" in payload
            or "result" not in payload
        ):
            raise QuoteError("RPC_READ_FAILED", "Invalid pinned Base RPC result")
        return payload["result"]

    async def call(self, address, signature, types=(), values=(), outputs=("uint256",)):
        key = repr((address.lower(), signature, types, values, outputs))
        if key not in self.calls:
            self.calls[key] = asyncio.create_task(
                super().call(address, signature, types, values, outputs)
            )
        result = await asyncio.shield(self.calls[key])
        if address.lower() == UNI_FACTORY.lower() and signature == "getPool(address,address,uint24)" and outputs == ("address",) and result and int(result[0], 16):
            self.canonical_pools.add(result[0].lower())
        return result

    async def quote(self, *args, **kwargs):
        result = await super().quote(*args, **kwargs)
        if result.get("gas_units_quote_estimate") is not None:
            result["gas_estimate_semantics"] = "Context-dependent read-only simulation; aggregate call warming can change this diagnostic. Not transaction gas or net-profit evidence."
        return result

    async def discover(self, tin, tout):
        key = tuple(sorted((tin, tout)))
        if key not in self.discovery:
            self.discovery[key] = asyncio.create_task(self._discover(tin, tout))
        return await asyncio.shield(self.discovery[key])

    async def _discover(self, tin, tout):
        pools = {}
        for pool in self.snapshot["pools"]:
            actual = {
                str((pool.get(k) or {}).get("address", "")).lower()
                for k in ("baseToken", "quoteToken")
            }
            if actual != {tin, tout}:
                continue
            labels = pool.get("labels") or []
            if not isinstance(labels, list) or not all(
                isinstance(label, str) for label in labels
            ):
                continue
            supported = (
                pool.get("dexId") == "uniswap"
                and "v3" in [label.lower() for label in labels]
            ) or (
                pool.get("dexId") == "aerodrome"
                and not any(
                    "slip" in label.lower() or label.lower().startswith("cl")
                    for label in labels
                )
            )
            if supported:
                pools[pool["pairAddress"].lower()] = {
                    "pool": pool["pairAddress"].lower(),
                    "dex": pool["dexId"],
                    "labels": labels,
                }

        async def factory(fee):
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
                return (
                    {
                        "pool": pool.lower(),
                        "dex": "uniswap",
                        "labels": ["v3"],
                        "fee": fee,
                    }
                    if int(pool, 16)
                    else None
                )
            except QuoteError:
                return None

        for pool in await asyncio.gather(
            *(factory(fee) for fee in (100, 500, 3000, 10000))
        ):
            if pool:
                pools[pool["pool"]] = pool
        # Canonical factory pools come before provider-labelled candidates. Otherwise
        # an unsupported CL/v4 listing can crowd every working V3 adapter out.
        return sorted(
            pools.values(),
            key=lambda pool: (0 if "fee" in pool else 1, pool.get("fee", 0)),
        )[:3]

    async def direct_quotes(self, tin, tout, raw):
        pools = await self.discover(tin, tout)

        async def quote(pool):
            try:
                return await self.quote(pool, tin, tout, raw)
            except QuoteError:
                return None

        return [
            quote
            for quote in await asyncio.gather(*(quote(pool) for pool in pools))
            if quote
        ]

    async def paths(self, tin, tout, raw):
        key = (tin, tout, raw)
        if key not in self.route_quotes:
            self.route_quotes[key] = asyncio.create_task(self._paths(tin, tout, raw))
        return await asyncio.shield(self.route_quotes[key])

    async def _paths(self, tin, tout, raw):
        paths = [
            {
                "amount_in_raw": str(raw),
                "amount_out_raw": quote["amount_out_raw"],
                "legs": [quote],
            }
            for quote in await self.direct_quotes(tin, tout, raw)
        ]
        weth = TOKENS["WETH"]
        if weth not in (tin, tout):
            for first in (await self.direct_quotes(tin, weth, raw))[:2]:
                for second in (
                    await self.direct_quotes(weth, tout, int(first["amount_out_raw"]))
                )[:2]:
                    if first["pool"] != second["pool"]:
                        paths.append(
                            {
                                "amount_in_raw": str(raw),
                                "amount_out_raw": second["amount_out_raw"],
                                "legs": [first, second],
                            }
                        )
        return sorted(paths, key=lambda path: int(path["amount_out_raw"]), reverse=True)


async def _new_cycle(client):
    snapshot = await market_snapshot(client)
    engine = CycleQuotes(client, snapshot)
    try:
        return await _initialize_cycle(engine)
    except BaseException:
        await engine.close_pending()
        raise


async def _initialize_cycle(engine):
    engine.observed_at_ms = int(time.time() * 1000)
    await engine.setup(TOKENS["USDC"], TOKENS["WETH"])
    if (
        not isinstance(engine.block_info, dict)
        or engine.block_info.get("number") != engine.block
        or not __import__("re").fullmatch(
            r"0x[0-9a-fA-F]{64}", str(engine.block_info.get("hash", ""))
        )
    ):
        raise QuoteError(
            "INVALID_BLOCK", "Provider did not bind the exact quoted block"
        )
    block_age = time.time() - int(engine.block_info["timestamp"], 16)
    if not -5 <= block_age <= 10:
        raise QuoteError("STALE_BLOCK", "Provider head is too old for a live feed")

    async def decimals(token):
        try:
            return (
                token,
                (await engine.call(token, "decimals()", outputs=("uint8",)))[0],
            )
        except QuoteError:
            return token, None

    for token, decimal in await asyncio.gather(
        *(
            decimals(token)
            for key, token in TOKENS.items()
            if key not in ("USDC", "WETH")
        )
    ):
        if isinstance(decimal, int) and 0 <= decimal <= 255:
            engine.decimals[token] = decimal
    return engine


def quote_freshness(engine):
    """Check ages again after quote acquisition, without renewing their timestamp."""
    now_ms = int(time.time() * 1000)
    quote_age_ms = now_ms - engine.observed_at_ms
    block_timestamp_ms = int(engine.block_info["timestamp"], 16) * 1000
    block_age_ms = now_ms - block_timestamp_ms
    if not -1_000 <= quote_age_ms < MAX_QUOTE_AGE_MS:
        raise QuoteError("STALE_QUOTE", "Original quote acquisition has expired")
    if not -5_000 <= block_age_ms <= MAX_BLOCK_AGE_MS:
        raise QuoteError("STALE_BLOCK", "Quoted block exceeded the source-age ceiling")
    return {
        "quote_observed_at_ms": engine.observed_at_ms,
        "block_timestamp_ms": block_timestamp_ms,
        "block_age_at_completion_ms": block_age_ms,
        "maximum_quote_age_ms": MAX_QUOTE_AGE_MS,
        "maximum_source_block_age_ms": MAX_BLOCK_AGE_MS,
    }


async def acquire_quote_cycle(client):
    global _CYCLE
    if getattr(client, "is_closed", False):
        raise QuoteError("CLIENT_CLOSED", "Quote acquisition HTTP client is closed")
    if (
        _CYCLE is not None
        and _CYCLE.client is client
        and 0 <= int(time.time() * 1000) - _CYCLE.observed_at_ms < MAX_QUOTE_AGE_MS
    ):
        return _CYCLE
    key = id(client)
    entry = _CYCLE_TASKS.get(key)
    if entry is None:
        entry = {"client": client, "task": asyncio.create_task(_new_cycle(client)), "released": False}
        _CYCLE_TASKS[key] = entry
    task = entry["task"]
    try:
        cycle = await asyncio.shield(task)
        if entry["released"] or getattr(client, "is_closed", False):
            raise QuoteError("CLIENT_CLOSED", "Quote acquisition HTTP client closed during setup")
        _CLIENT_CYCLES[key] = cycle
        _CYCLE = cycle
        return _CYCLE
    finally:
        if task.done() and _CYCLE_TASKS.get(key) is entry:
            _CYCLE_TASKS.pop(key)


async def release_quote_cycle(client):
    """Cancel this client's setup and read tasks before HTTP scope teardown."""
    global _CYCLE
    entry = _CYCLE_TASKS.pop(id(client), None)
    cycle = _CLIENT_CYCLES.pop(id(client), None)
    if _CYCLE is not None and _CYCLE.client is client:
        cycle = cycle or _CYCLE
        _CYCLE = None
    if entry is not None:
        entry["released"] = True
        task = entry["task"]
        if not task.done():
            task.cancel()
        results = await asyncio.gather(task, return_exceptions=True)
        if results and not isinstance(results[0], BaseException):
            cycle = results[0]
    if cycle is not None and callable(getattr(cycle, "close_pending", None)):
        await cycle.close_pending()


def balance_delta(pool, timestamp_ms, balances):
    history = _BALANCE_HISTORY.setdefault(pool, deque(maxlen=120))
    prior = next(
        (row for row in reversed(history) if timestamp_ms - row[0] >= 60_000), None
    )
    history.append((timestamp_ms, balances))
    if prior is None:
        return {"status": "warming_up", "observed_window_seconds": 0, "delta_raw": None}
    return {
        "status": "measured",
        "observed_window_seconds": (timestamp_ms - prior[0]) / 1000,
        "delta_raw": {
            token: str(int(value) - int(prior[1][token]))
            for token, value in balances.items()
        },
        "scope": "Pool ERC20 balance change includes swaps, LP flows and fees; it is not attributed netflow",
    }


async def fetch_base_liquidity_state(client):
    engine = await acquire_quote_cycle(client)

    async def asset(symbol, token):
        markets = [
            p
            for p in engine.snapshot["pools"]
            if str((p.get("baseToken") or {}).get("address", "")).lower() == token
        ]
        if not markets:
            return None
        market = max(
            markets, key=lambda p: _number((p.get("liquidity") or {}).get("usd")) or 0
        )
        quotes = []
        if token in engine.decimals:
            sizes = ("1", "100", "1000", "10000")
            acquired_paths = await asyncio.gather(
                *(
                    engine.paths(
                        TOKENS["USDC"],
                        token,
                        int(size) * 10 ** engine.decimals[TOKENS["USDC"]],
                    )
                    for size in sizes
                )
            )
            for size, paths in zip(sizes, acquired_paths):
                if paths:
                    quotes.append(
                        {
                            "notional_usdc": size,
                            "amount_out_raw": paths[0]["amount_out_raw"],
                            "minimum_output_raw": str(
                                int(paths[0]["amount_out_raw"]) * 9950 // 10000
                            ),
                            "slippage_tolerance_bps": 50,
                            "route": paths[0],
                            "decimals_out": engine.decimals[token],
                        }
                    )
        reference = next((q for q in quotes if q["notional_usdc"] == "1"), None)
        for quote in quotes:
            quote["price_impact_vs_one_usdc_bps"] = (
                float(
                    (
                        1
                        - Decimal(quote["amount_out_raw"])
                        / Decimal(quote["notional_usdc"])
                        / Decimal(reference["amount_out_raw"])
                    )
                    * 10000
                )
                if reference
                else None
            )
        timestamp = min(engine.observed_at_ms, engine.snapshot["timestamp_ms"])
        balances = {}
        delta = None
        pool = market["pairAddress"].lower()
        try:
            for key in ("baseToken", "quoteToken"):
                address = market[key]["address"].lower()
                balances[address] = str(
                    (
                        await engine.call(
                            address, "balanceOf(address)", ("address",), (pool,)
                        )
                    )[0]
                )
            delta = balance_delta(pool, timestamp, balances)
        except QuoteError:
            pass
        return {
            "symbol": symbol,
            "token_address": token,
            "primary_dex": market.get("dexId"),
            "pair_address": pool,
            "price_usd": _number(market.get("priceUsd")),
            "liquidity_usd": _number((market.get("liquidity") or {}).get("usd")),
            "volume_24h_usd": _number((market.get("volume") or {}).get("h24")),
            "price_delta_5m_pct": (market.get("priceChange") or {}).get("m5"),
            "pool_token_balances_raw": balances or None,
            "pool_balance_change": delta,
            "netflow_velocity_60s_usd": None,
            "buy_tax_bps": None,
            "sell_tax_bps": None,
            "is_honeypot": None,
            "slippage_quotes_bps": None,
            "size_quotes_usdc": [q for q in quotes if q["notional_usdc"] != "1"],
            "timestamp_ms": timestamp,
            "ttl_seconds": 10,
            "evidence": {
                "market_provider": "DexScreener",
                "market_acquired_at_ms": engine.snapshot["timestamp_ms"],
                "quote_method": "read_only_eth_call",
                "block_number": int(engine.block, 16),
                "block_hash": engine.block_info["hash"],
                "provider": engine.selected_provider,
                "pool_balance_scope": "ERC20 balances, not active curve liquidity",
                "quote_scope": "Acquired direct or WETH two-hop routes; not executed swaps or USD-peg proof",
                "transfer_tax_status": "not_simulated",
                **quote_freshness(engine),
            },
        }

    rows = await asyncio.gather(
        *(asset(symbol, token) for symbol, token in TOKENS.items() if symbol != "USDC"),
        return_exceptions=True,
    )
    valid = [row for row in rows if isinstance(row, dict)]
    quote_freshness(engine)
    if not any(row["size_quotes_usdc"] for row in valid):
        raise QuoteError(
            "QUOTE_ACQUISITION_FAILED",
            "No exact-amount quote was acquired; market summaries alone do not fulfill this feed",
        )
    return valid
