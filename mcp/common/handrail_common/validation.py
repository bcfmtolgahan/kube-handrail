"""Argument guards. Each check raises GuardError carrying a contract error code."""

import re
import uuid
from collections.abc import Collection, Mapping
from typing import Any

from handrail_common.envelope import FORBIDDEN_NAMESPACE, INVALID_ARGUMENT, OUT_OF_BOUNDS

# Namespaces are DNS-1123 labels; Deployment names are DNS-1123 subdomains.
_DNS1123_LABEL = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS1123_SUBDOMAIN = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)


class GuardError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def require_namespace(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 63 or not _DNS1123_LABEL.match(value):
        raise GuardError(INVALID_ARGUMENT, "namespace must be a DNS-1123 label")
    return value


def require_name(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 253 or not _DNS1123_SUBDOMAIN.match(value):
        raise GuardError(INVALID_ARGUMENT, "name must be a DNS-1123 subdomain")
    return value


def require_int(value: Any, field: str) -> int:
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        raise GuardError(INVALID_ARGUMENT, f"{field} must be an integer")
    return value


def require_str(args: Mapping[str, Any], field: str) -> str:
    value = args.get(field)
    if not isinstance(value, str) or not value:
        raise GuardError(INVALID_ARGUMENT, f"{field} must be a non-empty string")
    return value


def check_namespace_allowed(namespace: str, allowed: Collection[str]) -> None:
    if namespace not in allowed:
        raise GuardError(FORBIDDEN_NAMESPACE, f"namespace {namespace!r} is not allowed")


def check_replica_bounds(replicas: int, minimum: int, maximum: int) -> None:
    if not minimum <= replicas <= maximum:
        raise GuardError(OUT_OF_BOUNDS, f"replicas must be between {minimum} and {maximum}")


def validate_scale_args(
    args: Mapping[str, Any], allowed: Collection[str], minimum: int, maximum: int
) -> tuple[str, str, int]:
    """Guard checks 1-3 for a scale request; returns (namespace, name, replicas)."""
    if not isinstance(args, Mapping):
        raise GuardError(INVALID_ARGUMENT, "arguments must be an object")
    namespace = require_namespace(args.get("namespace"))
    name = require_name(args.get("name"))
    replicas = require_int(args.get("replicas"), "replicas")
    check_namespace_allowed(namespace, allowed)
    check_replica_bounds(replicas, minimum, maximum)
    return namespace, name, replicas


def require_plan_id(value: Any) -> str:
    """Plan ids are lowercase canonical UUIDs, exactly as issued."""
    try:
        canonical = str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        canonical = None
    if canonical is None or canonical != value:
        raise GuardError(INVALID_ARGUMENT, "plan_id must be a UUID")
    return value
