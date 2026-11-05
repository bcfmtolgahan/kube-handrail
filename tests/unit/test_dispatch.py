import json
import logging
from types import SimpleNamespace

import pytest

from handrail_common.dispatch import caller_signature, dispatch, tool_name
from handrail_common.envelope import (
    CONFLICT,
    FORBIDDEN_NAMESPACE,
    INTERNAL,
    KUBERNETES_ERROR,
    UNKNOWN_TOOL,
    err,
    ok,
)
from handrail_common.observability import (
    correlation_id,
    current_caller,
    current_channel,
    current_correlation_id,
)
from handrail_common.validation import GuardError

KEY = "test-caller-key"


def signed(caller: str) -> str:
    """The reserved argument as the interceptor sets it."""
    return f"{caller}.{caller_signature(KEY, caller)}"


def ctx(name):
    custom = None if name is None else {"bedrockAgentCoreToolName": name}
    return SimpleNamespace(client_context=SimpleNamespace(custom=custom))


@pytest.mark.parametrize(
    "full, expected",
    [
        ("k8sr___get_workload_status", "get_workload_status"),
        ("k8sw___apply_scale_deployment", "apply_scale_deployment"),
        ("get_workload_status", None),
        ("k8sr___", None),
        (None, None),
    ],
)
def test_tool_name(full, expected):
    assert tool_name(ctx(full)) == expected


def test_tool_name_without_client_context():
    assert tool_name(SimpleNamespace(client_context=None)) is None
    assert tool_name(None) is None


def test_routes_to_handler():
    handlers = {"echo": lambda args: ok(args)}
    assert dispatch(handlers, {"a": 1}, ctx("t___echo")) == ok({"a": 1})


def test_tool_name_argument_is_ignored():
    handlers = {"echo": lambda args: ok(args)}
    result = dispatch(handlers, {"tool_name": "echo"}, ctx(None))
    assert result["error"]["code"] == UNKNOWN_TOOL


def test_unknown_tool():
    result = dispatch({}, {}, ctx("t___nope"))
    assert result["ok"] is False and result["error"]["code"] == UNKNOWN_TOOL


def test_guard_error_maps_to_code():
    def handler(args):
        raise GuardError(FORBIDDEN_NAMESPACE, "no")

    result = dispatch({"x": handler}, {}, ctx("t___x"))
    assert result["error"] == {"code": FORBIDDEN_NAMESPACE, "message": "no"}


def test_guard_error_details_pass_through():
    details = {"generation": {"planned": 4, "current": 5}}

    def handler(args):
        raise GuardError(CONFLICT, "changed", details)

    result = dispatch({"x": handler}, {}, ctx("t___x"))
    assert result["error"] == {"code": CONFLICT, "message": "changed", "details": details}


def test_err_includes_details_only_when_given():
    assert err(CONFLICT, "changed")["error"] == {"code": CONFLICT, "message": "changed"}
    assert err(CONFLICT, "changed", {})["error"] == {"code": CONFLICT, "message": "changed"}
    assert err(CONFLICT, "changed", {"a": 1})["error"]["details"] == {"a": 1}


def test_exception_is_internal_without_details(caplog):
    def handler(args):
        raise RuntimeError("secret detail")

    with caplog.at_level(logging.ERROR):
        result = dispatch({"x": handler}, {}, ctx("t___x"))
    assert result["error"]["code"] == INTERNAL
    assert "secret detail" not in str(result)
    assert "secret detail" in caplog.text


# --- Correlation id and the tool record ---------------------------------------

PLAN_ID = "8f7c1a2e-0000-4000-8000-0000000000aa"


def full_ctx(custom: dict | None = None, request_id: str | None = "lambda-req") -> SimpleNamespace:
    return SimpleNamespace(
        client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": "t___x", **(custom or {})}
        ),
        aws_request_id=request_id,
    )


@pytest.mark.parametrize(
    ("custom", "request_id", "expected"),
    [
        (
            {"bedrockAgentCoreAwsRequestId": "gw-req", "bedrockAgentCoreMcpMessageId": "7"},
            "lambda-req",
            "gw-req",
        ),
        ({"bedrockAgentCoreAwsRequestId": 7}, "lambda-req", "7"),
        # The client-chosen MCP message id is never the correlation id.
        ({"bedrockAgentCoreMcpMessageId": "msg-1"}, "lambda-req", "lambda-req"),
        ({"kubeHandrailCorrelationId": "approver-req"}, "lambda-req", "approver-req"),
        ({}, "lambda-req", "lambda-req"),
        ({}, None, None),
        # Values that are not short tokens are skipped, never logged or annotated.
        ({"bedrockAgentCoreAwsRequestId": "two\nlines"}, "lambda-req", "lambda-req"),
        ({"bedrockAgentCoreAwsRequestId": "x" * 129}, "lambda-req", "lambda-req"),
        ({"bedrockAgentCoreAwsRequestId": True}, "lambda-req", "lambda-req"),
    ],
)
def test_correlation_id_fallbacks(custom, request_id, expected):
    assert correlation_id(full_ctx(custom, request_id)) == expected


def test_correlation_id_without_client_context():
    assert correlation_id(SimpleNamespace(client_context=None, aws_request_id="r")) == "r"
    assert correlation_id(None) is None


def records(capsys) -> list[dict]:
    return [json.loads(line) for line in capsys.readouterr().out.splitlines()]


def test_handler_sees_the_correlation_id(capsys):
    seen = []
    handlers = {"x": lambda args: seen.append(current_correlation_id()) or ok({})}
    dispatch(handlers, {}, full_ctx({"bedrockAgentCoreAwsRequestId": "gw-req"}))
    assert seen == ["gw-req"]
    assert current_correlation_id() is None


def test_one_record_per_successful_call(capsys):
    handlers = {"x": lambda args: ok({"plan_id": PLAN_ID})}
    env = {"CALLER_INTERCEPTOR": "true", "CALLER_KEY": KEY, "METRIC_NAMESPACE": "kh/tools"}
    event = {"_kube_handrail_caller": signed("client-1")}
    dispatch(handlers, event, full_ctx({"bedrockAgentCoreAwsRequestId": "gw-req"}), env)

    (record,) = records(capsys)
    assert record["_aws"]["CloudWatchMetrics"] == [
        {
            "Namespace": "kh/tools",
            "Dimensions": [["Tool"], []],
            "Metrics": [
                {"Name": "ToolCalls", "Unit": "Count"},
                {"Name": "ToolErrors", "Unit": "Count"},
            ],
        }
    ]
    assert isinstance(record["_aws"]["Timestamp"], int)
    assert isinstance(record.pop("duration_ms"), int)
    del record["_aws"]
    assert record == {
        "Tool": "x",
        "ToolCalls": 1,
        "ToolErrors": 0,
        "tool": "x",
        "correlation_id": "gw-req",
        "caller": "client-1",
        "plan_id": PLAN_ID,
        "outcome": "ok",
        "reason": None,
        "scope": None,
    }


def test_guard_rejection_is_counted_per_code(capsys):
    def handler(args):
        raise GuardError(CONFLICT, "changed", {"generation": {"planned": 1, "current": 2}})

    dispatch({"x": handler}, {"plan_id": PLAN_ID}, full_ctx(), {})
    (record,) = records(capsys)
    metrics = record["_aws"]["CloudWatchMetrics"]
    assert metrics[0]["Namespace"] == "kube-handrail/tools"
    assert metrics[1] == {
        "Namespace": "kube-handrail/tools",
        "Dimensions": [["Tool", "Code"]],
        "Metrics": [{"Name": "GuardRejections", "Unit": "Count"}],
    }
    assert (record["Code"], record["GuardRejections"], record["ToolErrors"]) == (CONFLICT, 1, 0)
    assert (record["outcome"], record["plan_id"], record["caller"]) == (CONFLICT, PLAN_ID, None)
    assert record["correlation_id"] == "lambda-req"


@pytest.mark.parametrize(
    "error, outcome",
    [
        (RuntimeError("boom"), INTERNAL),
        (GuardError(INTERNAL, "history unavailable"), INTERNAL),
        # A failing Kubernetes API (RBAC, access entry, 5xx) must reach the alarm.
        (GuardError(KUBERNETES_ERROR, "Kubernetes API error (403)"), KUBERNETES_ERROR),
    ],
)
def test_errors_are_tool_errors_not_rejections(capsys, error, outcome):
    def handler(args):
        raise error

    dispatch({"x": handler}, {}, full_ctx(), {})
    (record,) = records(capsys)
    assert (record["outcome"], record["ToolErrors"]) == (outcome, 1)
    assert "GuardRejections" not in record and "Code" not in record
    assert len(record["_aws"]["CloudWatchMetrics"]) == 1


def test_unknown_tool_is_recorded(capsys):
    dispatch({}, {}, ctx("t___nope"))
    (record,) = records(capsys)
    assert (record["Tool"], record["tool"], record["Code"]) == ("unknown", None, UNKNOWN_TOOL)


def test_record_never_carries_argument_text(capsys):
    marker = "MARKER-free-text-7f3a"

    def handler(args):
        raise GuardError(FORBIDDEN_NAMESPACE, "no")

    event = {"namespace": marker, "name": marker, "plan_id": marker, "reason": marker}
    custom = {"bedrockAgentCoreAwsRequestId": marker + " with spaces"}
    dispatch({"x": handler}, event, full_ctx(custom), {})
    out = capsys.readouterr().out
    assert marker not in out
    assert json.loads(out)["plan_id"] is None


def test_unverified_caller_is_null(capsys):
    dispatch({"x": lambda args: ok({})}, {"_kube_handrail_caller": "forged"}, full_ctx(), {})
    (record,) = records(capsys)
    assert record["caller"] is None
    assert "forged" not in json.dumps(record)


def test_caller_and_channel_are_bound_only_during_the_call(capsys):
    seen = []
    handlers = {"x": lambda args: seen.append((current_caller(), current_channel())) or ok({})}
    env = {"CALLER_INTERCEPTOR": "true", "CALLER_KEY": KEY}
    dispatch(handlers, {"_kube_handrail_caller": signed("client-1")}, full_ctx(), env)
    custom = {"kubeHandrailChannel": "approval-api"}
    dispatch(handlers, {}, full_ctx(custom), env)
    assert seen == [("client-1", "gateway"), (None, "approval-api")]
    assert (current_caller(), current_channel()) == (None, "gateway")


def test_a_forged_caller_is_ignored_in_legacy_mode(capsys):
    seen = []
    handlers = {"x": lambda args: seen.append((current_caller(), args)) or ok({})}
    dispatch(handlers, {"_kube_handrail_caller": signed("forged"), "a": 1}, full_ctx(), {})
    assert seen == [(None, {"a": 1})]


@pytest.mark.parametrize(
    "value",
    [
        "client-1",  # a gateway without its interceptor passes the client's own value
        "client-1.",
        f"client-1.{'0' * 64}",
        f"client-2.{caller_signature(KEY, 'client-1')}",
        f"client-1.{caller_signature('another-key', 'client-1')}",
        f".{caller_signature(KEY, '')}",
        "client-1.\u00e9",
        f"client-1.{'0' * 63}\u00e9",
        "client-1.\ud800",
        ["client-1"],
    ],
)
def test_an_unsigned_or_forged_caller_is_not_trusted_in_identity_mode(capsys, value):
    seen = []
    handlers = {"x": lambda args: seen.append((current_caller(), args)) or ok({})}
    env = {"CALLER_INTERCEPTOR": "true", "CALLER_KEY": KEY}
    dispatch(handlers, {"_kube_handrail_caller": value, "a": 1}, full_ctx(), env)
    assert seen == [(None, {"a": 1})]
    (record,) = records(capsys)
    assert record["caller"] is None


def test_without_a_key_no_caller_is_trusted(capsys):
    seen = []
    handlers = {"x": lambda args: seen.append(current_caller()) or ok({})}
    env = {"CALLER_INTERCEPTOR": "true"}
    dispatch(handlers, {"_kube_handrail_caller": signed("client-1")}, full_ctx(), env)
    dispatch(handlers, {"_kube_handrail_caller": "client-1"}, full_ctx(), env)
    assert seen == [None, None]


@pytest.mark.parametrize("value", ["verifier", "APPROVAL-API", "", 1, None])
def test_any_other_channel_value_is_the_gateway(capsys, value):
    seen = []
    handlers = {"x": lambda args: seen.append(current_channel()) or ok({})}
    dispatch(handlers, {}, full_ctx({"kubeHandrailChannel": value}), {})
    assert seen == ["gateway"]


def test_rejection_reason_and_scope_are_recorded(capsys):
    def handler(args):
        raise GuardError("forbidden", "no", {"reason": "not_requester"})

    dispatch({"x": handler}, {}, full_ctx(), {})
    (record,) = records(capsys)
    assert (record["Code"], record["reason"], record["scope"]) == (
        "forbidden",
        "not_requester",
        None,
    )
