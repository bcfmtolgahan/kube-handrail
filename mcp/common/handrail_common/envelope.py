"""Response envelope and error codes from mcp/CONTRACT.md."""

from typing import Any

INVALID_ARGUMENT = "invalid_argument"
UNKNOWN_TOOL = "unknown_tool"
NOT_FOUND = "not_found"
FORBIDDEN_NAMESPACE = "forbidden_namespace"
OUT_OF_BOUNDS = "out_of_bounds"
GITOPS_MANAGED = "gitops_managed"
PLAN_NOT_FOUND = "plan_not_found"
PLAN_EXPIRED = "plan_expired"
PLAN_USED = "plan_used"
PLAN_MISMATCH = "plan_mismatch"
CONFLICT = "conflict"
REMEDIATION_DISABLED = "remediation_disabled"
KUBERNETES_ERROR = "kubernetes_error"
INTERNAL = "internal"

ERROR_CODES = frozenset(
    {
        INVALID_ARGUMENT,
        UNKNOWN_TOOL,
        NOT_FOUND,
        FORBIDDEN_NAMESPACE,
        OUT_OF_BOUNDS,
        GITOPS_MANAGED,
        PLAN_NOT_FOUND,
        PLAN_EXPIRED,
        PLAN_USED,
        PLAN_MISMATCH,
        CONFLICT,
        REMEDIATION_DISABLED,
        KUBERNETES_ERROR,
        INTERNAL,
    }
)

Envelope = dict[str, Any]


def ok(data: Any, truncated: bool = False) -> Envelope:
    return {"ok": True, "data": data, "error": None, "truncated": truncated}


def err(code: str, message: str) -> Envelope:
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code: {code}")
    return {
        "ok": False,
        "data": None,
        "error": {"code": code, "message": message},
        "truncated": False,
    }
