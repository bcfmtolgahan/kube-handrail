"""Correlation ID and the per-invocation tool record (mcp/CONTRACT.md, Observability)."""

import json
import re
import time
from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

from handrail_common.envelope import INTERNAL

# Tried in this order. The gateway sets the first two in client_context.custom; the
# approver sets the third when it runs an apply, so the apply shares its id.
GATEWAY_REQUEST_ID_KEY = "bedrockAgentCoreAwsRequestId"
MCP_MESSAGE_ID_KEY = "bedrockAgentCoreMcpMessageId"
APPROVER_CORRELATION_KEY = "kubeHandrailCorrelationId"
CORRELATION_KEYS = (GATEWAY_REQUEST_ID_KEY, MCP_MESSAGE_ID_KEY, APPROVER_CORRELATION_KEY)
CORRELATION_ANNOTATION = "kube-handrail.io/correlation-id"
DEFAULT_METRIC_NAMESPACE = "kube-handrail/tools"
UNKNOWN_TOOL_DIMENSION = "unknown"
OK = "ok"

# The MCP message id is chosen by the client, so only a short token is accepted: the value
# goes into logs, the plan record and a Kubernetes annotation.
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


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


def tool_record(
    tool: str | None,
    correlation: str | None,
    caller: str | None,
    plan_id: str | None,
    outcome: str,
    duration_ms: int,
    namespace: str,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """One log line per tool invocation, also a CloudWatch embedded metric format record.

    Only these fixed fields are logged, never the arguments. `internal` counts as an error;
    every other error code is a guard rejection, counted per code.
    """
    rejected = outcome not in (OK, INTERNAL)
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
        "ToolErrors": int(outcome == INTERNAL),
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
        "duration_ms": duration_ms,
    }


def emit(record: Mapping[str, Any]) -> None:
    # print, not logging: the runtime's log format prefixes each line, and an embedded
    # metric record must be the whole line.
    print(json.dumps(record, default=str), flush=True)
