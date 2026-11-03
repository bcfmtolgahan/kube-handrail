"""Per-object oscillation breaker (mcp/CONTRACT.md, History checks).

History is the plan table, read through the object-history index. An object is identified
by namespace, name and uid, so a Deployment recreated under the same name starts over.
Every check fails closed: an error reading the history propagates and the request fails.
"""

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from handrail_common.config import Settings
from handrail_common.envelope import (
    COOLDOWN,
    FLAPPING,
    MANUAL_CHANGE,
    OBJECT_BUDGET_EXCEEDED,
    PLAN_RATE_LIMITED,
)
from handrail_common.plans import RESULT_APPLIED, HistoryEntry, Plan, PlanStore
from handrail_common.validation import GuardError

LAST_PLAN_ANNOTATION = "kube-handrail.io/last-plan-id"
BUDGET_WINDOW_SECONDS = 3600
# The result of an apply is recorded after the patch, so applied_at can trail expires_at by
# up to one Lambda run. History is read and kept with this margin.
APPLY_GRACE_SECONDS = 60
# Most recent applies listed in error details.
DETAIL_HISTORY_LIMIT = 20


def retention_seconds(settings: Settings) -> int:
    """How long a plan record must stay in the table to serve as history."""
    window = max(
        BUDGET_WINDOW_SECONDS,
        settings.flap_window_seconds,
        settings.plan_rate_window_seconds,
        settings.apply_cooldown_seconds,
    )
    return window + settings.plan_ttl_seconds + APPLY_GRACE_SECONDS


def _sign(value: int) -> int:
    return (value > 0) - (value < 0)


def _within(timestamp: int, now: int, window: int) -> bool:
    # An event exactly `window` seconds old has left the window.
    return timestamp > now - window


def applied_within(entries: Sequence[HistoryEntry], now: int, window: int) -> list[HistoryEntry]:
    """Applied plans whose applied_at falls in the window, oldest apply first."""
    applied = [e for e in entries if e.applied and _within(e.applied_at or 0, now, window)]
    return sorted(applied, key=lambda e: (e.applied_at or 0, e.plan_id))


def _history(applied: Sequence[HistoryEntry]) -> list[dict[str, int]]:
    return [
        {"applied_at": e.applied_at or 0, "from": e.replicas_from, "to": e.replicas_to}
        for e in applied[-DETAIL_HISTORY_LIMIT:]
    ]


def check_flapping(
    entries: Sequence[HistoryEntry], now: int, window: int, replicas_from: int, replicas_to: int
) -> None:
    """Reject a second reversal of direction within the window. A single revert is allowed.

    Applies that left the replica count unchanged have no direction and are skipped.
    """
    direction = _sign(replicas_to - replicas_from)
    applied = applied_within(entries, now, window)
    directions = [d for d in (_sign(e.replicas_to - e.replicas_from) for e in applied) if d]
    if direction == 0 or len(directions) < 2:
        return
    last, before = directions[-1], directions[-2]
    if direction == -last and last == -before:
        raise GuardError(
            FLAPPING,
            f"this change would reverse direction a second time within {window} s; "
            "wait for the window to pass or fix the cause",
            {
                "window_seconds": window,
                "count": len(applied),
                "requested": {"from": replicas_from, "to": replicas_to},
                "history": _history(applied),
            },
        )


def check_object_budget(entries: Sequence[HistoryEntry], now: int, limit: int) -> None:
    applied = applied_within(entries, now, BUDGET_WINDOW_SECONDS)
    if len(applied) >= limit:
        raise GuardError(
            OBJECT_BUDGET_EXCEEDED,
            f"{len(applied)} applies to this object in the last hour; the limit is {limit}",
            {
                "window_seconds": BUDGET_WINDOW_SECONDS,
                "limit": limit,
                "count": len(applied),
                "history": _history(applied),
            },
        )


def check_cooldown(entries: Sequence[HistoryEntry], now: int, cooldown: int) -> None:
    """Reject a change to an object kube-handrail changed less than `cooldown` seconds ago."""
    recent = applied_within(entries, now, cooldown)
    if recent:
        last = recent[-1].applied_at or 0
        raise GuardError(
            COOLDOWN,
            f"this object was changed {now - last} s ago; wait {cooldown} s between changes",
            {
                "cooldown_seconds": cooldown,
                "last_applied_at": last,
                "retry_after": last + cooldown,
                "history": _history(recent),
            },
        )


def check_plan_rate(entries: Sequence[HistoryEntry], now: int, window: int, limit: int) -> None:
    created = [e for e in entries if _within(e.created_at, now, window)]
    if len(created) >= limit:
        raise GuardError(
            PLAN_RATE_LIMITED,
            f"{len(created)} plans for this object in the last {window} s; the limit is {limit}",
            {
                "window_seconds": window,
                "limit": limit,
                "count": len(created),
                "history": _history(applied_within(entries, now, window)),
            },
        )


def with_last_applied(entries: Sequence[HistoryEntry], plan: Plan, now: int) -> list[HistoryEntry]:
    """The history with the plan the Deployment's last-plan annotation names counted as applied.

    The index is eventually consistent and the apply result is recorded best-effort, so the
    most recent apply can be missing or marked unapplied there. The annotation is set by the
    patch itself and read from the API server, so it is authoritative for that one apply.
    When no apply time was recorded, the latest time it can have happened is used.
    """
    applied_at = plan.applied_at if plan.applied_at is not None else min(plan.expires_at, now)
    last = HistoryEntry(
        plan_id=plan.plan_id,
        replicas_from=plan.replicas_from,
        replicas_to=plan.replicas_to,
        created_at=plan.created_at,
        result=RESULT_APPLIED,
        applied_at=applied_at,
    )
    return [e for e in entries if e.plan_id != plan.plan_id] + [last]


def check_history(
    store: PlanStore,
    key: str,
    now: int,
    settings: Settings,
    replicas_from: int,
    replicas_to: int,
    *,
    plan_rate: bool,
    last_applied: Plan | None = None,
) -> None:
    """Cooldown, flapping and the hourly budget, plus the plan rate limit when `plan_rate` is set.

    `last_applied` is the plan named by the Deployment's annotation (last_applied_plan); it
    is merged into the history so the most recent apply is never missed.
    """
    entries = store.history(key, now - retention_seconds(settings))
    if last_applied is not None and last_applied.object_key == key:
        entries = with_last_applied(entries, last_applied, now)
    check_cooldown(entries, now, settings.apply_cooldown_seconds)
    check_flapping(entries, now, settings.flap_window_seconds, replicas_from, replicas_to)
    check_object_budget(entries, now, settings.object_hourly_apply_budget)
    if plan_rate:
        check_plan_rate(entries, now, settings.plan_rate_window_seconds, settings.plan_rate_limit)


def last_plan_id(deployment: Any) -> str | None:
    """The plan id kube-handrail last applied to this object, if the annotation holds one."""
    annotations = getattr(getattr(deployment, "metadata", None), "annotations", None) or {}
    value = annotations.get(LAST_PLAN_ANNOTATION) if isinstance(annotations, Mapping) else None
    try:
        canonical = str(uuid.UUID(value)) if isinstance(value, str) else None
    except ValueError:
        return None
    return value if canonical == value else None


def last_applied_plan(store: PlanStore, deployment: Any) -> Plan | None:
    """The plan kube-handrail last applied to this object, by its annotation, or None.

    None when the annotation is absent or not a UUID, the plan is no longer in the table or
    was never consumed, or it was made for another object with the same name (other uid).
    A read error propagates: the checks that need the plan must not run without it.
    """
    plan_id = last_plan_id(deployment)
    if plan_id is None:
        return None
    plan = store.consumed_plan(plan_id)
    if plan is None or plan.uid != str(deployment.metadata.uid):
        return None
    return plan


def check_manual_change(last_applied: Plan | None, current: int, override: bool) -> bool:
    """Reject undoing a change made outside kube-handrail since its last apply.

    Returns True when a manual change was found and `override` allowed it.
    """
    if last_applied is None or last_applied.replicas_to == current:
        return False
    if override:
        return True
    raise GuardError(
        MANUAL_CHANGE,
        "replicas were changed outside kube-handrail since its last apply; "
        "plan with override_manual_change=true to change them anyway",
        {"last_applied_to": last_applied.replicas_to, "current": current},
    )
