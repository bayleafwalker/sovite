"""Bounded MCP envelope errors preserve valid JSON-RPC request IDs."""
import json

import pytest

from test_workflow_admission import CALLER, installed, request


def rpc(platform, payload):
    # Escape surrogate inputs so the request reaches server-side validation.
    return request(platform, "POST", "/mcp", token=CALLER,
                   content=json.dumps(payload).encode("utf-8"))


@pytest.mark.parametrize("request_id", [True, ["PRIVATE-ID-CANARY"],
    {"secret": "PRIVATE-ID-CANARY"}, 0.5, "\ud800", "\udfff"])
def test_invalid_request_id_is_rejected_without_echo(installed, request_id):
    platform, _ = installed
    response = rpc(platform, {"jsonrpc": "2.0", "id": request_id, "method": "ping"})
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32600, "message": "Invalid request"}}
    assert "PRIVATE-ID-CANARY" not in response.text


@pytest.mark.parametrize("request_id", [None, 0, -1, 123, "ascii", "föø", "你好"])
def test_valid_request_id_is_preserved(installed, request_id):
    platform, _ = installed
    response = rpc(platform, {"jsonrpc": "2.0", "id": request_id, "method": "ping"})
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": request_id, "result": {}}


@pytest.mark.parametrize("version", [[], {"secret": "PRIVATE-VERSION-CANARY"}, 1, True, None])
def test_nonstring_protocol_version_is_bounded_invalid_params(installed, version):
    platform, _ = installed
    response = rpc(platform, {"jsonrpc": "2.0", "id": "initialize-request", "method": "initialize",
                             "params": {"protocolVersion": version}})
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": "initialize-request",
                              "error": {"code": -32602, "message": "Invalid parameters"}}
    assert "PRIVATE-VERSION-CANARY" not in response.text


@pytest.mark.parametrize("params,expected", [
    ({}, "2025-11-25"),
    ({"protocolVersion": "2025-03-26"}, "2025-03-26"),
    ({"protocolVersion": "2025-06-18"}, "2025-06-18"),
    ({"protocolVersion": "2025-11-25"}, "2025-11-25"),
    ({"protocolVersion": "unknown-version"}, "2025-11-25"),
])
def test_protocol_negotiation_preserves_supported_and_default_behavior(installed, params, expected):
    platform, _ = installed
    response = rpc(platform, {"jsonrpc": "2.0", "id": 42, "method": "initialize", "params": params})
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 42 and body["result"]["protocolVersion"] == expected
