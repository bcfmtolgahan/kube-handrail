"""Correlation ID and the per-invocation tool record (mcp/CONTRACT.md, Observability)."""

import json
import re
import time
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from handrail_common.envelope import INTERNAL, KUBERNETES_ERROR

# Tried in this order. The gateway sets the first in client_context.custom; the approver
# sets the second when it runs an apply, so the apply shares its id. The gateway's MCP
# message id is not used: the client chooses it, so it is not unique across sessions.
GATEWAY_REQUEST_ID_KEY = "bedrockAgentCoreAwsRequestId"
APPROVER_CORRELATION_KEY = "kubeHandrailCorrelationId"
CORRELATION_KEYS = (GATEWAY_REQUEST_ID_KEY, APPROVER_CORRELATION_KEY)
# The channel an invocation came through (mcp/CONTRACT.md, Agent identity). Only the approver
# sets it, next to its correlation id; the gateway never does, and a gateway client cannot
# set client_context.custom. Anything but an exact known value counts as the gateway, which
# is the channel that requires a verified caller.
CHANNEL_KEY = "kubeHandrailChannel"
CHANNEL_GATEWAY = "gateway"
CHANNEL_APPROVAL_API = "approval-api"
CORRELATION_ANNOTATION = "kube-handrail.io/correlation-id"
DEFAULT_METRIC_NAMESPACE = "kube-handrail/tools"
UNKNOWN_TOOL_DIMENSION = "unknown"
OK = "ok"
# Outcomes counted as ToolErrors (the alarm) instead of GuardRejections: the tool could not
# do its job, as opposed to a guard refusing the request.
ERROR_OUTCOMES = (INTERNAL, KUBERNETES_ERROR)

# Only a short token is accepted: the value goes into logs, the plan record and a
# Kubernetes annotation.
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_caller: ContextVar[str | None] = ContextVar("caller", default=None)
_channel: ContextVar[str] = ContextVar("channel", default=CHANNEL_GATEWAY)


def _valid(value: Any) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if isinstance(value, str) and _CORRELATION_ID.match(value):
        return value
    return None


def correlation_id(context: Any) -> str | None:
    """The first valid id from the client context, else the Lambda request id."""
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None)
    if isinstance(custom, Mapping):
        for key in CORRELATION_KEYS:
            value = _valid(custom.get(key))
            if value is not None:
                return value
    return _valid(getattr(context, "aws_request_id", None))


def current_correlation_id() -> str | None:
    """The correlation id of the invocation dispatch is running, or None outside one."""
    return _correlation_id.get()


def bind_correlation_id(value: str | None) -> Any:
    """Set the id for the current invocation; pass the result to `unbind_correlation_id`."""
    return _correlation_id.set(value)


def unbind_correlation_id(token: Any) -> None:
    _correlation_id.reset(token)


def channel(context: Any) -> str:
    """The channel named in the client context: approval-api only when set exactly so."""
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None)
    if isinstance(custom, Mapping) and custom.get(CHANNEL_KEY) == CHANNEL_APPROVAL_API:
        return CHANNEL_APPROVAL_API
    return CHANNEL_GATEWAY


def current_caller() -> str | None:
    """The verified caller (an OAuth client_id) of the invocation dispatch is running, or
    None: outside one, without the interceptor, or when the gateway passed none."""
    return _caller.get()


def current_channel() -> str:
    """The channel of the invocation dispatch is running; the gateway outside one."""
    return _channel.get()


def bind_caller(caller: str | None, channel_name: str) -> tuple[Any, Any]:
    """Set the caller and channel for the current invocation; pass the result to
    `unbind_caller`."""
    return _caller.set(caller), _channel.set(channel_name)


def unbind_caller(tokens: tuple[Any, Any]) -> None:
    _caller.reset(tokens[0])
    _channel.reset(tokens[1])


def tool_record(
    tool: str | None,
    correlation: str | None,
    caller: str | None,
    plan_id: str | None,
    outcome: str,
    duration_ms: int,
    namespace: str,
    now_ms: int | None = None,
    reason: str | None = None,
    scope: str | None = None,
) -> dict[str, Any]:
    """One log line per tool invocation, also a CloudWatch embedded metric format record.

    Only these fixed fields are logged, never the arguments. `internal` and
    `kubernetes_error` count as errors; every other error code is a guard rejection,
    counted per code. `reason` and `scope` are the rejection's `details.reason` (forbidden)
    and `details.scope` (remediation_disabled), fixed words the server set.
    """
    rejected = outcome != OK and outcome not in ERROR_OUTCOMES
    metrics: list[dict[str, Any]] = [
        {
            "Namespace": namespace,
            # The empty set adds a metric across all tools, which the alarm watches.
            "Dimensions": [["Tool"], []],
            "Metrics": [
                {"Name": "ToolCalls", "Unit": "Count"},
                {"Name": "ToolErrors", "Unit": "Count"},
            ],
        }
    ]
    record: dict[str, Any] = {
        "Tool": tool or UNKNOWN_TOOL_DIMENSION,
        "ToolCalls": 1,
        "ToolErrors": int(outcome in ERROR_OUTCOMES),
    }
    if rejected:
        metrics.append(
            {
                "Namespace": namespace,
                "Dimensions": [["Tool", "Code"]],
                "Metrics": [{"Name": "GuardRejections", "Unit": "Count"}],
            }
        )
        record |= {"Code": outcome, "GuardRejections": 1}
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    return {
        "_aws": {"Timestamp": timestamp, "CloudWatchMetrics": metrics},
        **record,
        "tool": tool,
        "correlation_id": correlation,
        "caller": caller,
        "plan_id": plan_id,
        "outcome": outcome,
        "reason": reason,
        "scope": scope,
        "duration_ms": duration_ms,
    }


def emit(record: Mapping[str, Any]) -> None:
    # print, not logging: the runtime's log format prefixes each line, and an embedded
    # metric record must be the whole line.
    print(json.dumps(record, default=str), flush=True)
