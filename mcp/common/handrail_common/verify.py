"""Verification of an applied plan (mcp/CONTRACT.md, Verification and revert): one reading of
the Deployment at verify_at, and the outcome get_plan_outcome reports.

`evaluate` answers one question: did this change reach its target within the approved
window? It does not compare with the state before the change, and any sign that someone
else changed the Deployment since makes the outcome unknown, never degraded.
"""

from collections.abc import Mapping
from typing import Any

from handrail_common.approval import derived_status
from handrail_common.breaker import last_plan_id
from handrail_common.plans import KIND_REVERT, Plan
from handrail_common.untrusted import untrusted_text

HEALTHY = "healthy"
DEGRADED = "degraded"
UNKNOWN = "unknown"
# The plan was never consumed, or its apply did not patch the Deployment.
NOT_APPLIED = "not_applied"
# Derived by get_plan_outcome, never stored: the check has not run yet.
PENDING = "pending"

# unknown: the reading cannot say whether this change worked.
KUBERNETES_ERROR = "kubernetes_error"
NOT_FOUND = "not_found"
REPLACED = "replaced"
SUPERSEDED = "superseded"
SPEC_CHANGED = "spec_changed"
PAUSED = "paused"
NOT_OBSERVED = "not_observed"
LATE = "late"
# Derived by get_plan_outcome, never stored.
NOT_SCHEDULED = "not_scheduled"
CHECK_OVERDUE = "check_overdue"
# degraded and healthy.
PROGRESS_DEADLINE_EXCEEDED = "progress_deadline_exceeded"
REPLICA_FAILURE = "replica_failure"
TARGET_NOT_REACHED = "target_not_reached"
TARGET_REACHED = "target_reached"

# revert.status in get_plan_outcome; reverted, skipped and refused are stored.
REVERT_NOT_REQUESTED = "not_requested"
REVERT_PENDING = "pending"
REVERT_NOT_NEEDED = "not_needed"
REVERTED = "reverted"
REVERT_SKIPPED = "skipped"
REVERT_REFUSED = "refused"

# The API server defaults spec.replicas to 1 when it is omitted.
DEFAULT_REPLICAS = 1
# A condition reason is stored cut to this length; get_plan_outcome returns it as untrusted
# text. Messages are never stored.
REASON_MAX_CHARS = 256


def _conditions(status: Any) -> dict[str, Any]:
    return {c.type: c for c in (getattr(status, "conditions", None) or [])}


def _condition_is(conditions: Mapping[str, Any], kind: str, value: str) -> bool:
    condition = conditions.get(kind)
    return condition is not None and condition.status == value


def _target_reached(plan: Plan, status: Any, conditions: Mapping[str, Any]) -> bool:
    available = getattr(status, "available_replicas", None) or 0
    total = getattr(status, "replicas", None) or 0
    target = plan.replicas_to
    if target == 0:
        return total == 0
    if not _condition_is(conditions, "Available", "True"):
        return False
    if plan.template_from == plan.template_to:
        # A scale: enough pods of the one template are available.
        return available >= target
    # A new pod template rolled out, as kubectl rollout status decides it: every pod is
    # updated, no old pod is left, and every updated pod is available.
    updated = getattr(status, "updated_replicas", None) or 0
    return updated >= target and total <= updated and available >= updated


def evaluate(plan: Plan, deployment: Any) -> tuple[str, str]:
    """(outcome, reason) of one reading of the Deployment (a client model) the plan applied
    to. The first matching row wins: every unknown before any degraded."""
    meta, spec, status = deployment.metadata, deployment.spec, deployment.status
    if str(meta.uid) != plan.uid:
        return UNKNOWN, REPLACED
    if last_plan_id(deployment) != plan.plan_id:
        return UNKNOWN, SUPERSEDED
    generation = int(meta.generation)
    replicas = DEFAULT_REPLICAS if spec.replicas is None else spec.replicas
    # The apply bumped the generation exactly once; any other spec change bumped it again.
    # An unchanged generation also means the pod template is still the one written.
    if generation != plan.generation + 1 or replicas != plan.replicas_to:
        return UNKNOWN, SPEC_CHANGED
    if getattr(spec, "paused", None) is True:
        # Kubernetes does not check the progress deadline of a paused rollout.
        return UNKNOWN, PAUSED
    if (getattr(status, "observed_generation", None) or 0) < generation:
        return UNKNOWN, NOT_OBSERVED
    conditions = _conditions(status)
    progressing = conditions.get("Progressing")
    if (
        progressing is not None
        and progressing.status == "False"
        and progressing.reason == "ProgressDeadlineExceeded"
    ):
        return DEGRADED, PROGRESS_DEADLINE_EXCEEDED
    if _condition_is(conditions, "ReplicaFailure", "True"):
        return DEGRADED, REPLICA_FAILURE
    if not _target_reached(plan, status, conditions):
        return DEGRADED, TARGET_NOT_REACHED
    return HEALTHY, TARGET_REACHED


def observed(deployment: Any) -> dict[str, Any]:
    """The counts and conditions the outcome was decided on: each condition's type, status
    and reason, never its message."""
    spec, status = deployment.spec, deployment.status
    replicas = DEFAULT_REPLICAS if spec.replicas is None else spec.replicas
    return {
        "desired": replicas,
        "replicas": getattr(status, "replicas", None) or 0,
        "ready": getattr(status, "ready_replicas", None) or 0,
        "available": getattr(status, "available_replicas", None) or 0,
        "updated": getattr(status, "updated_replicas", None) or 0,
        "generation": int(deployment.metadata.generation),
        "observed_generation": getattr(status, "observed_generation", None) or 0,
        "conditions": [
            {
                "type": str(c.type),
                "status": str(c.status),
                "reason": None if c.reason is None else str(c.reason)[:REASON_MAX_CHARS],
            }
            for c in (getattr(status, "conditions", None) or [])
        ],
    }


def _observed_view(value: Any) -> tuple[Any, bool]:
    """The stored observation with each condition reason as untrusted text: anyone who can
    write a Deployment's status sets it."""
    if not isinstance(value, Mapping):
        return None, False
    view = dict(value)
    truncated = False
    conditions = []
    for condition in value.get("conditions") or []:
        reason, cut = untrusted_text(condition.get("reason"))
        truncated = truncated or cut
        conditions.append({**condition, "reason": reason})
    view["conditions"] = conditions
    return view, truncated


def verification_view(plan: Plan, now: int, grace: int) -> tuple[dict[str, Any], bool]:
    """The verification state of a plan record, and whether a field was cut."""
    view: dict[str, Any] = {
        "window_seconds": plan.verify_window_seconds,
        "verify_at": plan.verify_at,
    }
    if plan.verification is not None:
        observation, truncated = _observed_view(plan.verification.get("observed"))
        return {
            **view,
            "status": plan.verification.get("outcome"),
            "reason": plan.verification.get("reason"),
            "checked_at": plan.verification.get("checked_at"),
            "observed": observation,
        }, truncated
    if plan.verify_at is None:
        # Consumed without a schedule: made before verification, or with it turned off.
        status, reason = (UNKNOWN, NOT_SCHEDULED) if plan.used else (NOT_APPLIED, None)
    elif now > plan.verify_at + grace:
        status, reason = UNKNOWN, CHECK_OVERDUE
    else:
        status, reason = PENDING, None
    return {**view, "status": status, "reason": reason, "checked_at": None, "observed": None}, False


def revert_view(plan: Plan, verification: str | None) -> dict[str, Any]:
    """The revert state, given the verification status. A revert record is never reverted."""
    if plan.kind == KIND_REVERT or not plan.revert_on_failure:
        return {"requested": False, "status": REVERT_NOT_REQUESTED}
    if plan.revert is not None:
        return {
            "requested": True,
            "status": plan.revert.get("status"),
            "reason": plan.revert.get("reason"),
            "revert_plan_id": plan.revert.get("revert_plan_id"),
        }
    pending = verification in (PENDING, DEGRADED)
    return {"requested": True, "status": REVERT_PENDING if pending else REVERT_NOT_NEEDED}


def outcome_view(record: Mapping[str, Any], now: int, grace: int) -> tuple[dict[str, Any], bool]:
    """get_plan_outcome's data from a plan record, and whether a field was cut."""
    plan = Plan.from_item(record)
    verification, truncated = verification_view(plan, now, grace)
    correlation = {
        step: value
        for step, value in (
            ("plan", plan.plan_correlation_id),
            ("apply", plan.apply_correlation_id),
            ("verification", (plan.verification or {}).get("correlation_id")),
        )
        if value is not None
    }
    return {
        "plan_id": plan.plan_id,
        "action": plan.action,
        "namespace": plan.namespace,
        "name": plan.name,
        "kind": plan.kind or "change",
        "reverts_plan_id": plan.reverts_plan_id,
        "replicas": {"from": plan.replicas_from, "to": plan.replicas_to},
        "plan_status": derived_status(record, now),
        "verification": verification,
        "revert": revert_view(plan, verification["status"]),
        "correlation_ids": correlation,
    }, truncated
