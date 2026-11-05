import base64
import importlib.util
import json
from pathlib import Path

from handrail_common.dispatch import CALLER_ARGUMENT, caller_signature, pop_caller

_SPEC = importlib.util.spec_from_file_location(
    "interceptor_handler", Path(__file__).parents[2] / "mcp" / "interceptor" / "handler.py"
)
interceptor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(interceptor)


def token(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def event(body, authorization: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {authorization}"} if authorization else {}
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {"gatewayRequest": {"headers": headers, "body": body}},
    }


def call(arguments) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "k8sr___x", "arguments": arguments},
    }


def transformed(result: dict):
    assert result["interceptorOutputVersion"] == "1.0"
    return result["mcp"]["transformedGatewayRequest"]["body"]


def test_tool_call_gets_the_verified_client_id():
    body = transformed(
        interceptor.lambda_handler(event(call({"replicas": 2}), token({"client_id": "abc"})), None)
    )
    assert body["params"]["arguments"] == {"replicas": 2, CALLER_ARGUMENT: "abc"}
    assert body["params"]["name"] == "k8sr___x"


def test_client_supplied_caller_is_overwritten():
    body = transformed(
        interceptor.lambda_handler(
            event(call({CALLER_ARGUMENT: "attacker"}), token({"client_id": "abc"})), None
        )
    )
    assert body["params"]["arguments"][CALLER_ARGUMENT] == "abc"


def test_unreadable_token_gives_unknown_never_the_client_value():
    for auth in (None, "not-a-jwt", "a.!!!.c", token({"sub": "x"})):
        body = transformed(
            interceptor.lambda_handler(event(call({CALLER_ARGUMENT: "attacker"}), auth), None)
        )
        assert body["params"]["arguments"][CALLER_ARGUMENT] == "unknown"


def test_lowercase_header_and_missing_arguments():
    raw = event({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "t"}})
    raw["mcp"]["gatewayRequest"]["headers"] = {
        "authorization": f"Bearer {token({'client_id': 'abc'})}"
    }
    assert transformed(interceptor.lambda_handler(raw, None))["params"]["arguments"] == {
        CALLER_ARGUMENT: "abc"
    }


def test_other_methods_pass_through_unchanged():
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    assert (
        transformed(interceptor.lambda_handler(event(body, token({"client_id": "abc"})), None))
        == body
    )


KEY = "test-caller-key"
IDENTITY = {"CALLER_INTERCEPTOR": "true", "CALLER_KEY": KEY}


def test_with_a_key_the_caller_is_signed(monkeypatch):
    monkeypatch.setenv("CALLER_KEY", KEY)
    body = transformed(
        interceptor.lambda_handler(
            event(call({CALLER_ARGUMENT: "attacker"}), token({"client_id": "abc"})), None
        )
    )
    assert body["params"]["arguments"][CALLER_ARGUMENT] == f"abc.{caller_signature(KEY, 'abc')}"
    body = transformed(interceptor.lambda_handler(event(call({}), None), None))
    assert (
        body["params"]["arguments"][CALLER_ARGUMENT]
        == f"unknown.{caller_signature(KEY, 'unknown')}"
    )


def test_interceptor_output_round_trips_through_pop_caller(monkeypatch):
    monkeypatch.setenv("CALLER_KEY", KEY)
    body = transformed(
        interceptor.lambda_handler(event(call({"a": 1}), token({"client_id": "abc"})), None)
    )
    arguments = dict(body["params"]["arguments"])
    assert pop_caller(arguments, IDENTITY) == "abc"
    assert arguments == {"a": 1}


def test_pop_caller_trusts_only_a_signed_argument_with_the_interceptor_enabled():
    args = {"replicas": 2, CALLER_ARGUMENT: f"abc.{caller_signature(KEY, 'abc')}"}
    assert pop_caller(dict(args), IDENTITY) == "abc"
    assert pop_caller(dict(args), {"CALLER_INTERCEPTOR": "true"}) is None
    assert pop_caller(dict(args), {**IDENTITY, "CALLER_INTERCEPTOR": "false"}) is None
    assert pop_caller(dict(args), {}) is None
    stripped = dict(args)
    pop_caller(stripped, {})
    assert stripped == {"replicas": 2}
    # What a client sends itself when the gateway lost its interceptor.
    assert pop_caller({CALLER_ARGUMENT: "abc"}, IDENTITY) is None
    assert pop_caller({CALLER_ARGUMENT: "abc.0123"}, IDENTITY) is None
    assert pop_caller({CALLER_ARGUMENT: 5}, IDENTITY) is None


def test_dispatch_never_hands_the_reserved_argument_to_a_tool():
    from types import SimpleNamespace

    from handrail_common.dispatch import dispatch

    seen = {}
    context = SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": "k8sr___t"})
    )
    dispatch(
        {"t": lambda args: seen.update(args) or {"ok": True}},
        {"a": 1, CALLER_ARGUMENT: "x"},
        context,
    )
    assert seen == {"a": 1}


def test_a_retried_request_gives_the_same_result():
    """The gateway may retry an interceptor; the result must not depend on earlier calls."""
    request = event(call({"replicas": 2}), token({"client_id": "abc"}))
    first = interceptor.lambda_handler(request, None)
    assert interceptor.lambda_handler(request, None) == first
    # Re-running it on its own output changes nothing either.
    again = event(transformed(first), token({"client_id": "abc"}))
    assert transformed(interceptor.lambda_handler(again, None)) == transformed(first)
