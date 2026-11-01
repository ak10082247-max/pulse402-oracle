import pytest
from starlette.testclient import TestClient
from app import app

client = TestClient(app)

def test_mcp_head_returns_200():
    res = client.head("/mcp")
    assert res.status_code == 200
    assert res.headers.get("content-type") == "application/json"

def test_mcp_head_trailing_slash_returns_200():
    res = client.head("/mcp/")
    assert res.status_code == 200

def test_mcp_empty_post_probe_returns_discovery_200():
    res = client.post("/mcp", content=b"")
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert "tools" in data["result"]

def test_mcp_empty_post_trailing_slash_returns_200():
    res = client.post("/mcp/", content=b"")
    assert res.status_code == 200
    data = res.json()
    assert data["jsonrpc"] == "2.0"
    assert "tools" in data["result"]

def test_sanitize_unpaid_returns_402_not_400():
    res = client.post("/v1/pulse/threat-intel/sanitize")
    assert res.status_code == 402
    assert "payment-required" in res.headers

def test_sanitize_with_text_to_sanitize_unpaid_returns_402():
    res = client.post("/v1/pulse/threat-intel/sanitize", json={"text_to_sanitize": "drop table tokens;"})
    assert res.status_code == 402
    assert "payment-required" in res.headers


def test_trial_feed_allows_three_free_calls_then_challenges_402():
    # Use distinct feed or test client to verify trial counter
    # 1st call: succeeds with 2 remaining
    res1 = client.get("/v1/trial/base-liquidity")
    assert res1.status_code == 200
    assert res1.headers.get("x-pulse-trial") == "active"
    assert res1.headers.get("x-pulse-trial-remaining") == "2"

    # 2nd call: succeeds with 1 remaining
    res2 = client.get("/v1/trial/base-liquidity")
    assert res2.status_code == 200
    assert res2.headers.get("x-pulse-trial-remaining") == "1"

    # 3rd call: succeeds with 0 remaining
    res3 = client.get("/v1/trial/base-liquidity")
    assert res3.status_code == 200
    assert res3.headers.get("x-pulse-trial-remaining") == "0"

    # 4th call: trial exhausted -> returns 402 challenge directing to paid endpoint!
    res4 = client.get("/v1/trial/base-liquidity")
    assert res4.status_code == 402
    assert "payment-required" in res4.headers
