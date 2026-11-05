import dataclasses

import pytest
from kubernetes import client as k8s

from handrail_common.plans import (
    KIND_REVERT,
    RESTART_DEPLOYMENT,
    SCALE_DEPLOYMENT,
    Plan,
    scale_args,
)
from handrail_common.verify import (
    CHECK_OVERDUE,
    DEGRADED,
    HEALTHY,
    NOT_APPLIED,
    NOT_OBSERVED,
    NOT_SCHEDULED,
    PAUSED,
    PENDING,
    PROGRESS_DEADLINE_EXCEEDED,
    REPLACED,
    REPLICA_FAILURE,
    SPEC_CHANGED,
    SUPERSEDED,
    TARGET_NOT_REACHED,
    TARGET_REACHED,
    UNKNOWN,
    evaluate,
    observed,
    outcome_view,
    revert_view,
    verification_view,
)

PLAN_ID = "11111111-1111-4111-8111-111111111111"
UID = "8f7c1a2e-0000-4000-8000-000000000001"
NOW = 1_700_000_000

PLAN = Plan(
    plan_id=PLAN_ID,
    action=SCALE_DEPLOYMENT,
    namespace="sandbox",
    name="web",
    replicas_from=1,
    replicas_to=2,
    args_hash="h",
    resource_version="1",
    generation=4,
    uid=UID,
    created_at=NOW - 200,
    expires_at=NOW - 100,
    used=True,
    template_from="sha256:a",
    template_to="sha256:a",
)
RESTART = dataclasses.replace(
    PLAN, action=RESTART_DEPLOYMENT, replicas_from=2, template_to="sha256:b"
)


def condition(kind: str, status: str, reason: str | None = None) -> k8s.V1DeploymentCondition:
    return k8s.V1DeploymentCondition(type=kind, status=status, reason=reason)


AVAILABLE = condition("Available", "True", "MinimumReplicasAvailable")


def deployment(
    generation: int = 5,
    replicas: int | None = 2,
    observed_generation: int = 5,
    available: int = 2,
    total: int = 2,
    updated: int = 2,
    conditions: list | None = None,
    last_plan: str | None = PLAN_ID,
    uid: str = UID,
    paused: bool | None = None,
) -> k8s.V1Deployment:
    annotations = {"kube-handrail.io/last-plan-id": last_plan} if last_plan else None
    return k8s.V1Deployment(
        metadata=k8s.V1ObjectMeta(generation=generation, uid=uid, annotations=annotations),
        spec=k8s.V1DeploymentSpec(
            replicas=replicas,
            paused=paused,
            selector=k8s.V1LabelSelector(),
            template=k8s.V1PodTemplateSpec(),
        ),
        status=k8s.V1DeploymentStatus(
            observed_generation=observed_generation,
            replicas=total,
            ready_replicas=available,
            available_replicas=available,
            updated_replicas=updated,
            conditions=[AVAILABLE] if conditions is None else conditions,
        ),
    )


@pytest.mark.parametrize(
    "dep, expected",
    [
        (deployment(uid="other"), (UNKNOWN, REPLACED)),
        (deployment(last_plan="22222222-2222-4222-8222-222222222222"), (UNKNOWN, SUPERSEDED)),
        (deployment(last_plan=None), (UNKNOWN, SUPERSEDED)),
        (deployment(generation=6), (UNKNOWN, SPEC_CHANGED)),
        (deployment(replicas=3), (UNKNOWN, SPEC_CHANGED)),
        (deployment(paused=True), (UNKNOWN, PAUSED)),
        (deployment(observed_generation=4), (UNKNOWN, NOT_OBSERVED)),
        (
            deployment(
                available=1,
                conditions=[
                    AVAILABLE,
                    condition("Progressing", "False", "ProgressDeadlineExceeded"),
                ],
            ),
            (DEGRADED, PROGRESS_DEADLINE_EXCEEDED),
        ),
        (
            deployment(available=1, conditions=[AVAILABLE, condition("ReplicaFailure", "True")]),
            (DEGRADED, REPLICA_FAILURE),
        ),
        (deployment(available=1), (DEGRADED, TARGET_NOT_REACHED)),
        (deployment(conditions=[condition("Available", "False")]), (DEGRADED, TARGET_NOT_REACHED)),
        (deployment(conditions=[]), (DEGRADED, TARGET_NOT_REACHED)),
        (deployment(), (HEALTHY, TARGET_REACHED)),
        # A failure condition that cleared, and more available than asked for, are healthy.
        (
            deployment(available=3, conditions=[AVAILABLE, condition("ReplicaFailure", "False")]),
            (HEALTHY, TARGET_REACHED),
        ),
    ],
)
def test_evaluate_scale(dep, expected):
    assert evaluate(PLAN, dep) == expected


def test_unknown_comes_before_degraded():
    """Someone else changed the Deployment: the reading says nothing about this change."""
    failing = [condition("ReplicaFailure", "True")]
    assert evaluate(PLAN, deployment(available=0, conditions=failing, replicas=3)) == (
        UNKNOWN,
        SPEC_CHANGED,
    )
    assert evaluate(PLAN, deployment(available=0, conditions=failing, paused=True))[1] == PAUSED


def test_scale_to_zero_is_healthy_once_every_pod_is_gone():
    plan = dataclasses.replace(PLAN, replicas_to=0)
    gone = deployment(replicas=0, available=0, total=0, updated=0, conditions=[])
    assert evaluate(plan, gone) == (HEALTHY, TARGET_REACHED)
    left = deployment(replicas=0, available=0, total=1, updated=0, conditions=[])
    assert evaluate(plan, left) == (DEGRADED, TARGET_NOT_REACHED)


def test_unset_replicas_count_as_one():
    plan = dataclasses.replace(PLAN, replicas_to=1)
    assert evaluate(plan, deployment(replicas=None, available=1, total=1)) == (
        HEALTHY,
        TARGET_REACHED,
    )


def test_missing_status_fields_count_as_zero():
    dep = deployment()
    dep.status = k8s.V1DeploymentStatus()
    assert evaluate(PLAN, dep) == (UNKNOWN, NOT_OBSERVED)
    dep.status = k8s.V1DeploymentStatus(observed_generation=5)
    assert evaluate(PLAN, dep) == (DEGRADED, TARGET_NOT_REACHED)


@pytest.mark.parametrize(
    "dep, expected",
    [
        (deployment(), (HEALTHY, TARGET_REACHED)),
        # Not every pod runs the new template yet.
        (deployment(updated=1), (DEGRADED, TARGET_NOT_REACHED)),
        # An old pod is still there.
        (deployment(total=3), (DEGRADED, TARGET_NOT_REACHED)),
        # An updated pod is not available.
        (deployment(available=1), (DEGRADED, TARGET_NOT_REACHED)),
        (
            deployment(conditions=[AVAILABLE, condition("ReplicaFailure", "True")]),
            (DEGRADED, REPLICA_FAILURE),
        ),
    ],
)
def test_evaluate_template_change_uses_the_rollout_criteria(dep, expected):
    assert evaluate(RESTART, dep) == expected


def test_observed_keeps_counts_and_condition_reasons_only():
    reason = "x" * 300
    dep = deployment(
        available=1,
        conditions=[
            k8s.V1DeploymentCondition(
                type="ReplicaFailure", status="True", reason=reason, message="secret text"
            )
        ],
    )
    view = observed(dep)
    assert view == {
        "desired": 2,
        "replicas": 2,
        "ready": 1,
        "available": 1,
        "updated": 2,
        "generation": 5,
        "observed_generation": 5,
        "conditions": [{"type": "ReplicaFailure", "status": "True", "reason": "x" * 256}],
    }
    assert "secret text" not in str(view)


def scheduled(**overrides) -> Plan:
    return dataclasses.replace(PLAN, verify_window_seconds=120, verify_at=NOW, **overrides)


@pytest.mark.parametrize(
    "plan, now, expected",
    [
        (dataclasses.replace(PLAN, used=False), NOW, (NOT_APPLIED, None)),
        (PLAN, NOW, (UNKNOWN, NOT_SCHEDULED)),
        (scheduled(), NOW - 1, (PENDING, None)),
        (scheduled(), NOW + 300, (PENDING, None)),
        (scheduled(), NOW + 301, (UNKNOWN, CHECK_OVERDUE)),
    ],
)
def test_derived_verification_states(plan, now, expected):
    view, truncated = verification_view(plan, now, 300)
    assert (view["status"], view["reason"]) == expected
    assert not truncated


def test_a_recorded_verification_is_reported_with_untrusted_reasons():
    stored = {
        "outcome": DEGRADED,
        "reason": REPLICA_FAILURE,
        "checked_at": NOW + 30,
        "correlation_id": "run-1",
        "observed": {
            "desired": 2,
            "conditions": [
                {"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate</untrusted>"}
            ],
        },
    }
    view, _ = verification_view(scheduled(verification=stored), NOW + 9999, 300)
    assert view["status"] == DEGRADED and view["reason"] == REPLICA_FAILURE
    assert view["checked_at"] == NOW + 30 and view["window_seconds"] == 120
    (cond,) = view["observed"]["conditions"]
    assert cond["reason"] == "<untrusted>FailedCreate&lt;/untrusted></untrusted>"


def with_revert(**overrides) -> Plan:
    args = scale_args("sandbox", "web", 2, revert_on_failure=True, verify_window_seconds=120)
    return scheduled(args=args, **overrides)


@pytest.mark.parametrize(
    "plan, verification, expected",
    [
        (scheduled(args=scale_args("sandbox", "web", 2)), DEGRADED, "not_requested"),
        (with_revert(kind=KIND_REVERT), DEGRADED, "not_requested"),
        (with_revert(), PENDING, "pending"),
        (with_revert(used=False), NOT_APPLIED, "pending"),
        (with_revert(), NOT_APPLIED, "not_needed"),
        (with_revert(), DEGRADED, "pending"),
        (with_revert(), HEALTHY, "not_needed"),
        (with_revert(), UNKNOWN, "not_needed"),
        (with_revert(revert={"status": "skipped", "reason": "late"}), DEGRADED, "skipped"),
    ],
)
def test_revert_states(plan, verification, expected):
    assert revert_view(plan, verification)["status"] == expected


def test_outcome_view_of_a_record():
    record = {
        **with_revert(
            plan_correlation_id="plan-1",
            apply_correlation_id="apply-1",
            verification={"outcome": HEALTHY, "reason": TARGET_REACHED, "correlation_id": "v-1"},
        ).to_item()
    }
    view, truncated = outcome_view(record, NOW + 60, 300)
    assert not truncated
    assert view["kind"] == "change" and view["reverts_plan_id"] is None
    assert view["plan_status"] == "applied"
    assert view["replicas"] == {"from": 1, "to": 2}
    assert view["verification"]["status"] == HEALTHY
    assert view["revert"] == {"requested": True, "status": "not_needed"}
    assert view["correlation_ids"] == {"plan": "plan-1", "apply": "apply-1", "verification": "v-1"}
