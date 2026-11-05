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
FLAPPING = "flapping"
OBJECT_BUDGET_EXCEEDED = "object_budget_exceeded"
PLAN_RATE_LIMITED = "plan_rate_limited"
MANUAL_CHANGE = "manual_change"
COOLDOWN = "cooldown"
GLOBAL_BUDGET_EXCEEDED = "global_budget_exceeded"
BLAST_RADIUS_EXCEEDED = "blast_radius_exceeded"
APPROVAL_REQUIRED = "approval_required"
APPROVAL_RATE_LIMITED = "approval_rate_limited"
PLAN_DECIDED = "plan_decided"
REVISION_NOT_FOUND = "revision_not_found"
FORBIDDEN = "forbidden"
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
        FLAPPING,
        OBJECT_BUDGET_EXCEEDED,
        PLAN_RATE_LIMITED,
        MANUAL_CHANGE,
        COOLDOWN,
        GLOBAL_BUDGET_EXCEEDED,
        BLAST_RADIUS_EXCEEDED,
        APPROVAL_REQUIRED,
        APPROVAL_RATE_LIMITED,
        PLAN_DECIDED,
        REVISION_NOT_FOUND,
        FORBIDDEN,
        KUBERNETES_ERROR,
        INTERNAL,
    }
)

Envelope = dict[str, Any]


def ok(data: Any, truncated: bool = False) -> Envelope:
    return {"ok": True, "data": data, "error": None, "truncated": truncated}


def err(code: str, message: str, details: dict[str, Any] | None = None) -> Envelope:
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code: {code}")
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return {"ok": False, "data": None, "error": error, "truncated": False}
