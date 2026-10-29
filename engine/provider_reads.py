"""Bounded unsigned GET recovery for two fixed public evidence providers only."""
import asyncio
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import math
import re
import time

import httpx

from .base_quotes import QuoteError

READ_BUDGET_SECONDS = 5.0
ATTEMPT_TIMEOUT_SECONDS = 2.25
RETRY_DELAY_SECONDS = 0.15
MAX_RESPONSE_BYTES = 2_000_000
RETRY_HTTP_STATUSES = {408, 429, 500, 502, 503, 504}
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_PREFIX = {"dexscreener": "DEXSCREENER", "goplus": "GOPLUS"}
_REASONS = {"TIMEOUT", "NETWORK", "HTTP_TRANSPORT", "INVALID_JSON", "INVALID_SCHEMA", "NO_EVIDENCE", "API_REJECTED", "RETRY_BUDGET_EXHAUSTED", "RESPONSE_TOO_LARGE"}
_DIAGNOSTICS = {}
_METRIC_KEYS = ("request_count", "retry_count", "last_failure_code", "last_failure_http_status", "last_failure_api_code", "last_failure_at_ms", "last_success_at_ms")


class ProviderReadError(QuoteError):
    def __init__(self, provider, reason, *, status=None, attempts=0, api_code=None):
        if reason not in _REASONS and not re.fullmatch(r"HTTP_[1-5][0-9]{2}", reason):
            raise ValueError("Provider diagnostic reason is not a fixed code")
        super().__init__(_PREFIX[provider] + "_" + reason, "Public provider evidence acquisition failed")
        self.provider = provider
        self.provider_http_status = status if type(status) is int and 100 <= status <= 599 else None
        self.provider_api_code = api_code if type(api_code) is int and -(2**31) <= api_code < 2**31 else None
        self.attempts = attempts


@dataclass(frozen=True)
class ProviderJSON:
    body: object
    retrieved_at_ms: int
    attempts: int


def _state(provider):
    if provider not in _PREFIX:
        raise ValueError("Unknown public evidence provider")
    return _DIAGNOSTICS.setdefault(provider, {"request_count": 0, "retry_count": 0, "last_failure_code": None, "last_failure_http_status": None, "last_failure_api_code": None, "last_failure_at_ms": None, "last_success_at_ms": None})


def provider_diagnostics():
    return {provider: {key: _state(provider).get(key) for key in _METRIC_KEYS} for provider in _PREFIX}


def provider_error(provider, reason, *, status=None, attempts=0, api_code=None):
    error = ProviderReadError(provider, reason, status=status, attempts=attempts, api_code=api_code)
    _state(provider).update(last_failure_code=error.code, last_failure_http_status=error.provider_http_status, last_failure_api_code=error.provider_api_code, last_failure_at_ms=int(time.time() * 1000))
    return error


def accept_provider_result(provider, retrieved_at_ms):
    state = _state(provider)
    state["last_success_at_ms"] = max(state["last_success_at_ms"] or 0, retrieved_at_ms)


def _request(provider, token, addresses):
    if provider == "dexscreener" and isinstance(token, str) and _ADDRESS.fullmatch(token) and addresses is None:
        return f"https://api.dexscreener.com/token-pairs/v1/base/{token}", None
    if provider == "goplus" and token is None and isinstance(addresses, list) and 1 <= len(addresses) <= 4 and all(isinstance(value, str) and _ADDRESS.fullmatch(value) for value in addresses):
        return "https://api.gopluslabs.io/api/v1/token_security/8453", {"contract_addresses": ",".join(addresses)}
    raise ValueError("Only fixed public Base evidence reads are supported")


def _retry_delay(response):
    value = response.headers.get("Retry-After") if response is not None else None
    if isinstance(value, str) and len(value) <= 80:
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                delay = RETRY_DELAY_SECONDS
        if math.isfinite(delay):
            return max(0.0, delay)
    return RETRY_DELAY_SECONDS


async def read_public_json(client, provider, *, token=None, addresses=None):
    url, params = _request(provider, token, addresses)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + READ_BUDGET_SECONDS
    for attempt in (1, 2):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise provider_error(provider, "RETRY_BUDGET_EXHAUSTED", attempts=attempt - 1)
        response = None
        _state(provider)["request_count"] += 1
        if attempt > 1:
            _state(provider)["retry_count"] += 1
        try:
            async with asyncio.timeout(min(ATTEMPT_TIMEOUT_SECONDS, remaining)):
                response = await client.get(url, params=params, timeout=min(ATTEMPT_TIMEOUT_SECONDS, remaining), follow_redirects=False)
                status = response.status_code
                if status != 200:
                    reason = "HTTP_" + str(status)
                    retryable = status in RETRY_HTTP_STATUSES
                elif len(response.content) > MAX_RESPONSE_BYTES:
                    raise provider_error(provider, "RESPONSE_TOO_LARGE", status=200, attempts=attempt)
                else:
                    retrieved_at = int(time.time() * 1000)
                    try:
                        body = response.json()
                    except (ValueError, UnicodeError):
                        reason, retryable = "INVALID_JSON", True
                    else:
                        if loop.time() >= deadline:
                            raise provider_error(provider, "RETRY_BUDGET_EXHAUSTED", status=200, attempts=attempt)
                        return ProviderJSON(body, retrieved_at, attempt)
        except (TimeoutError, httpx.TimeoutException):
            reason, retryable = "TIMEOUT", True
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            reason, retryable = "NETWORK", True
        except httpx.HTTPError:
            reason, retryable = "HTTP_TRANSPORT", False
        status = response.status_code if response is not None else None
        error = provider_error(provider, reason, status=status, attempts=attempt)
        if not retryable or attempt == 2:
            raise error
        delay = _retry_delay(response)
        # Honor upstream Retry-After. If it cannot fit this original budget,
        # fail truthfully instead of retrying early or extending the deadline.
        if delay + 0.05 >= deadline - loop.time():
            raise error
        await asyncio.sleep(delay)
    raise AssertionError("Bounded attempt loop did not terminate")
