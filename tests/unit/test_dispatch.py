import logging
from types import SimpleNamespace

import pytest

from handrail_common.dispatch import dispatch, tool_name
from handrail_common.envelope import CONFLICT, FORBIDDEN_NAMESPACE, INTERNAL, UNKNOWN_TOOL, err, ok
from handrail_common.validation import GuardError


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
