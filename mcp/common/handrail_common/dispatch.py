"""Route a Gateway Lambda invocation to a tool handler."""

import hashlib
import hmac
import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from handrail_common.envelope import INTERNAL, UNKNOWN_TOOL, Envelope, err
from handrail_common.observability import (
    CHANNEL_VERIFIER,
    DEFAULT_METRIC_NAMESPACE,
    OK,
    bind_caller,
    bind_correlation_id,
    channel,
    correlation_id,
    current_correlation_id,
    emit,
    tool_record,
    unbind_caller,
    unbind_correlation_id,
)
from handrail_common.validation import GuardError, require_plan_id

logger = logging.getLogger(__name__)

TOOL_NAME_KEY = "bedrockAgentCoreToolName"
TARGET_DELIMITER = "___"
# Reserved target of the internal handlers (mcp/CONTRACT.md, Dispatch): a tool name
# kh_internal___<name> runs INTERNAL_HANDLERS[<name>], and only on the verifier channel. No
# gateway target has this name (scripts/check_contract.py), and a gateway client can set
# neither the tool name nor the channel.
INTERNAL_TARGET = "kh_internal"
# Reserved argument the gateway request interceptor sets to the verified caller, signed with
# CALLER_KEY (CONTRACT.md, Agent identity): "<client_id>.<hex HMAC-SHA256 of client_id>". It is
# trusted only when CALLER_INTERCEPTOR is "true" and the signature matches; a client can send
# the argument itself, but without the key it cannot sign it.
CALLER_ARGUMENT = "_kube_handrail_caller"

Handler = Callable[[dict[str, Any]], Envelope]


def target_and_tool(context: Any) -> tuple[str | None, str | None]:
    """The target before `___` and the tool name after it, each None when absent."""
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None)
    if not isinstance(custom, Mapping):
        return None, None
    full = custom.get(TOOL_NAME_KEY)
    if not isinstance(full, str) or TARGET_DELIMITER not in full:
        return None, None
    target, name = full.split(TARGET_DELIMITER, 1)
    return target or None, name or None


def tool_name(context: Any) -> str | None:
    """Return the tool name after `<target>___`, or None when absent."""
    return target_and_tool(context)[1]


def select_handler(
    handlers: Mapping[str, Handler],
    internal: Mapping[str, Handler],
    target: str | None,
    name: str | None,
    channel_name: str,
) -> Handler | None:
    """An internal handler for the reserved target on the verifier channel; a tool handler
    for any other target on any other channel; otherwise None (unknown_tool)."""
    if not name:
        return None
    if (target == INTERNAL_TARGET) != (channel_name == CHANNEL_VERIFIER):
        return None
    return (internal if target == INTERNAL_TARGET else handlers).get(name)


def caller_signature(key: str, caller: str) -> str:
    """The interceptor's signature of a caller; mcp/interceptor computes the same."""
    return hmac.new(key.encode(), caller.encode(), hashlib.sha256).hexdigest()


def pop_caller(args: dict[str, Any], env: Mapping[str, str] | None = None) -> str | None:
    """Remove the reserved caller argument; return the caller only when the interceptor is
    enabled and signed it. Anything else, including a gateway without its interceptor that
    passes a client's own value through, gives None, which identity mode refuses."""
    env = os.environ if env is None else env
    value = args.pop(CALLER_ARGUMENT, None)
    key = env.get("CALLER_KEY", "")
    if env.get("CALLER_INTERCEPTOR") != "true" or not key or not isinstance(value, str):
        return None
    # Client IDs and signatures are ASCII; compare_digest raises on other text, outside the
    # handler's error handling, so such a value is no caller rather than a failed invocation.
    if not value.isascii():
        return None
    caller, _, signature = value.rpartition(".")
    if not caller or not hmac.compare_digest(signature, caller_signature(key, caller)):
        return None
    return caller


def _detail(envelope: Envelope, key: str) -> str | None:
    details = (envelope.get("error") or {}).get("details")
    value = details.get(key) if isinstance(details, Mapping) else None
    return value if isinstance(value, str) else None


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
    internal: Mapping[str, Handler] | None = None,
) -> Envelope:
    env = os.environ if env is None else env
    started = time.monotonic()
    target, name = target_and_tool(context)
    channel_name = channel(context)
    handler = select_handler(handlers, internal or {}, target, name, channel_name)
    args = dict(event) if isinstance(event, dict) else {}
    caller = pop_caller(args, env)
    correlation = correlation_id(context)
    token = bind_correlation_id(correlation)
    # Handlers read the caller and channel through observability, as the correlation id.
    tokens = bind_caller(caller, channel_name)
    try:
        envelope = _run(handler, name, args)
    finally:
        unbind_caller(tokens)
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
            reason=_detail(envelope, "reason"),
            scope=_detail(envelope, "scope"),
        )
    )
    return envelope
