"""Route a Gateway Lambda invocation to a tool handler."""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from handrail_common.envelope import INTERNAL, UNKNOWN_TOOL, Envelope, err
from handrail_common.validation import GuardError

logger = logging.getLogger(__name__)

TOOL_NAME_KEY = "bedrockAgentCoreToolName"
TARGET_DELIMITER = "___"

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


def dispatch(handlers: Mapping[str, Handler], event: Any, context: Any) -> Envelope:
    name = tool_name(context)
    handler = handlers.get(name) if name else None
    if handler is None:
        return err(UNKNOWN_TOOL, f"unknown tool: {name!r}")
    args = event if isinstance(event, dict) else {}
    try:
        return handler(args)
    except GuardError as exc:
        return err(exc.code, exc.message, exc.details)
    except Exception:
        logger.exception("tool %s failed", name)
        return err(INTERNAL, "internal error")
