"""Route a Gateway Lambda invocation to a tool handler."""

import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from handrail_common.envelope import INTERNAL, UNKNOWN_TOOL, Envelope, err
from handrail_common.observability import (
    DEFAULT_METRIC_NAMESPACE,
    OK,
    bind_correlation_id,
    correlation_id,
    current_correlation_id,
    emit,
    tool_record,
    unbind_correlation_id,
)
from handrail_common.validation import GuardError, require_plan_id

logger = logging.getLogger(__name__)

TOOL_NAME_KEY = "bedrockAgentCoreToolName"
TARGET_DELIMITER = "___"
# Reserved argument the gateway request interceptor sets to the verified caller (CONTRACT.md,
# Caller identity). It is trusted only when CALLER_INTERCEPTOR is "true"; otherwise a client
# could have sent it.
CALLER_ARGUMENT = "_kube_handrail_caller"

Handler = Callable[[dict[str, Any]], Envelope]


def tool_name(context: Any) -> str | None:
    """Return the tool name after `<target>___`, or None when absent."""
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None)
    if not isinstance(custom, Mapping):
        return None
    full = custom.get(TOOL_NAME_KEY)
    if not isinstance(full, str) or TARGET_DELIMITER not in full:
        return None
    name = full.split(TARGET_DELIMITER, 1)[1]
    return name or None


def pop_caller(args: dict[str, Any], env: Mapping[str, str] | None = None) -> str | None:
    """Remove the reserved caller argument; return it only when the interceptor is enabled."""
    env = os.environ if env is None else env
    value = args.pop(CALLER_ARGUMENT, None)
    if env.get("CALLER_INTERCEPTOR") != "true":
        return None
    return value if isinstance(value, str) and value else None


def logged_plan_id(envelope: Envelope, args: Mapping[str, Any]) -> str | None:
    """The plan a call made or named, as a validated UUID only: never argument text."""
    data = envelope.get("data")
    candidates = [data.get("plan_id") if isinstance(data, Mapping) else None, args.get("plan_id")]
    for value in candidates:
        try:
            return require_plan_id(value)
        except GuardError:
            continue
    return None


def _run(handler: Handler | None, name: str | None, args: dict[str, Any]) -> Envelope:
    if handler is None:
        return err(UNKNOWN_TOOL, f"unknown tool: {name!r}")
    try:
        return handler(args)
    except GuardError as exc:
        return err(exc.code, exc.message, exc.details)
    except Exception:
        # The print()ed tool record has no Lambda request id; the correlation id joins them.
        logger.exception("tool %s failed, correlation_id %s", name, current_correlation_id())
        return err(INTERNAL, "internal error")


def dispatch(
    handlers: Mapping[str, Handler],
    event: Any,
    context: Any,
    env: Mapping[str, str] | None = None,
) -> Envelope:
    env = os.environ if env is None else env
    started = time.monotonic()
    name = tool_name(context)
    handler = handlers.get(name) if name else None
    args = dict(event) if isinstance(event, dict) else {}
    caller = pop_caller(args, env)
    correlation = correlation_id(context)
    token = bind_correlation_id(correlation)
    try:
        envelope = _run(handler, name, args)
    finally:
        unbind_correlation_id(token)
    outcome = OK if envelope.get("ok") else str((envelope.get("error") or {}).get("code"))
    emit(
        tool_record(
            tool=name if handler is not None else None,
            correlation=correlation,
            caller=caller,
            plan_id=logged_plan_id(envelope, args),
            outcome=outcome,
            duration_ms=int((time.monotonic() - started) * 1000),
            namespace=env.get("METRIC_NAMESPACE", "").strip() or DEFAULT_METRIC_NAMESPACE,
        )
    )
    return envelope
