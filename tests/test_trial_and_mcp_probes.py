import pytest
from starlette.testclient import TestClient
from app import app

client = TestClient(app)

def test_trial_unknown_feed_returns_clean_404():
    """Verify that an unknown or template feed name returns 404 JSON, not an unhandled 500."""
    response = client.get("/v1/trial/{feed_name}")
    assert response.status_code == 404
    data = response.json()
    assert data.get("error") == "UNKNOWN_FEED"
    assert "valid_feeds" in data

    response_encoded = client.get("/v1/trial/%7Bfeed_name%7D")
    assert response_encoded.status_code == 404
    assert response_encoded.json().get("error") == "UNKNOWN_FEED"

    response_bogus = client.get("/v1/trial/not-a-real-feed")
    assert response_bogus.status_code == 404
    assert response_bogus.json().get("error") == "UNKNOWN_FEED"

def test_trial_post_method_allowed():
    """Verify that POST /v1/trial/{feed_name} is allowed and does not return 405 Method Not Allowed."""
    # Test with valid args for threat-intel
    response = client.post("/v1/trial/threat-intel", json={"text_to_sanitize": "test untrusted text"})
    # Must NOT be 405 Method Not Allowed; can be 200 (if trial remaining) or 402 (if exhausted)
    assert response.status_code in (200, 402)
    assert response.status_code != 405

    # Test with valid args for base-liquidity
    response_base = client.post("/v1/trial/base-liquidity", json={"symbol": "WETH"})
    assert response_base.status_code in (200, 402)
    assert response_base.status_code != 405

def test_mcp_empty_json_probe_returns_200():
    """Verify that POST /mcp with empty body or empty JSON object {} returns 200 OK with tools manifest."""
    # Empty string body
    resp_empty = client.post("/mcp", content=b"")
    assert resp_empty.status_code == 200
    assert "result" in resp_empty.json()

    # Empty JSON object {} (frequent automated indexer probe)
    resp_dict = client.post("/mcp", json={})
    assert resp_dict.status_code == 200
    assert "result" in resp_dict.json()
    assert resp_dict.json()["result"].get("name") == "pulse402-oracle"

    # Trailing slash /mcp/ with {}
    resp_slash = client.post("/mcp/", json={})
    assert resp_slash.status_code == 200
    assert "result" in resp_slash.json()
