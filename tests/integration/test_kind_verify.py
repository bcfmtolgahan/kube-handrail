"""Verification and revert, driven through the verifier Lambda (tests/e2e/e2e_gateway.py:
verify_tests and the cases it runs). Instead of waiting out each window, a case moves the
plan's verify_at one second into the past and runs the verifier once. The degraded cases
use BestEffort fixtures under a ResourceQuota that admits one BestEffort pod, as the live
test does."""

import pytest
from harness import (
    KILL_SWITCH,
    NAMESPACE,
    QUOTA,
    code,
    deployment_manifest,
    unique,
    wait_until,
)

PLAN = "k8sr___plan_scale_deployment"
OUTCOME = "k8sr___get_plan_outcome"
LAST_PLAN_ANNOTATION = "kube-handrail.io/last-plan-id"
CORRELATION_ANNOTATION = "kube-handrail.io/correlation-id"
WINDOW = 60


def outcome(kh, plan_id):
    data = kh.call(OUTCOME, {"plan_id": plan_id})["data"]
    return data["verification"]["status"], data["revert"]["status"], data


def verify(kh, plan_id):
    kh.make_due(plan_id)
    run = kh.run_verifier()
    results = {r["plan_id"]: r for r in run["results"]}
    assert plan_id in results, run
    return results[plan_id], run


def test_healthy_change(kh):
    target = kh.create(deployment_manifest(unique("it-verify-ok"), surge_all=True))
    kh.wait_available(target["name"], 1)
    extra = {"verify_window_seconds": WINDOW}
    plan_id = kh.plan(target, 2, **extra)
    assert code(kh.apply(target, plan_id, 2, verify_window_seconds=90)) == "plan_mismatch"
    assert kh.apply(target, plan_id, 2, **extra)["ok"] is True
    status, revert, data = outcome(kh, plan_id)
    assert (status, revert) == ("pending", "not_requested")
    assert data["verification"]["window_seconds"] == WINDOW
    # Not due yet: the verifier leaves it alone.
    assert kh.run_verifier()["Due"] == 0

    kh.wait_available(target["name"], 2)
    result, _ = verify(kh, plan_id)
    assert result["outcome"] == "healthy"
    status, revert, data = outcome(kh, plan_id)
    assert (status, data["verification"]["reason"]) == ("healthy", "target_reached")
    # Out of the queue once final.
    kh.make_due(plan_id)
    assert kh.run_verifier()["Due"] == 0


def test_verification_arguments(kh):
    target = kh.create(deployment_manifest(unique("it-verify-args")))
    envelope = kh.call(PLAN, {**target, "replicas": 2, "verify_window_seconds": 30})
    assert code(envelope) == "invalid_argument"
    envelope = kh.call(PLAN, {**target, "replicas": 2, "revert_on_failure": True})
    assert code(envelope) == "invalid_argument"


def test_change_by_hand_inside_the_window_is_unknown(kh):
    target = kh.create(deployment_manifest(unique("it-verify-manual")))
    plan_id = kh.plan(target, 2, verify_window_seconds=WINDOW)
    assert kh.apply(target, plan_id, 2, verify_window_seconds=WINDOW)["ok"] is True
    kh.scale_by_hand(target["name"], 3)
    result, _ = verify(kh, plan_id)
    assert (result["outcome"], result["reason"]) == ("unknown", "spec_changed")
    assert kh.deployment(target["name"]).spec.replicas == 3


@pytest.fixture
def best_effort(kh):
    """A one-replica BestEffort fixture whose pod is available; scaling it to 2 leaves the
    second pod refused by the quota."""

    def create(prefix):
        wait_until("the BestEffort quota to be free", lambda: _quota_used(kh) == 0)
        target = kh.create(deployment_manifest(unique(prefix), best_effort=True))
        kh.wait_available(target["name"], 1)
        return target

    return create


def _quota_used(kh):
    quota = kh.core.read_namespaced_resource_quota(QUOTA, NAMESPACE)
    return int(((quota.status and quota.status.used) or {}).get("pods", "0"))


def _apply_degrading(kh, target, revert=True):
    extra = {"verify_window_seconds": WINDOW}
    if revert:
        extra["revert_on_failure"] = True
    plan_id = kh.plan(target, 2, **extra)
    # Applied 30 s ago, so the revert lands after it in the history, as it would after a
    # real window.
    kh.write_offset = -30
    assert kh.apply(target, plan_id, 2, **extra)["ok"] is True
    kh.write_offset = 0
    kh.wait_observed(target["name"])
    wait_until(
        "the quota to refuse the second pod",
        lambda: any(
            c.type == "ReplicaFailure" and c.status == "True"
            for c in kh.deployment(target["name"]).status.conditions or []
        ),
        timeout=30,
    )
    return plan_id


def test_degraded_change_is_reverted(kh, best_effort):
    target = best_effort("it-verify-bad")
    plan_id = _apply_degrading(kh, target)
    result, run = verify(kh, plan_id)
    assert result["outcome"] == "degraded"
    assert result["reason"] in ("replica_failure", "target_not_reached")
    assert result["revert"] == "reverted" and run["Reverts"] == 1
    status, revert, data = outcome(kh, plan_id)
    revert_id = data["revert"]["revert_plan_id"]
    assert (status, revert) == ("degraded", "reverted") and revert_id
    deployment = kh.deployment(target["name"])
    assert deployment.spec.replicas == 1
    assert deployment.metadata.annotations[LAST_PLAN_ANNOTATION] == revert_id
    assert deployment.metadata.annotations[CORRELATION_ANNOTATION] == run["correlation_id"]
    record = kh.record(revert_id)
    assert record["reverts_plan_id"] == plan_id and record["requested_by"] == "system:revert"

    # The revert is verified like any apply, and is never reverted itself.
    kh.wait_available(target["name"], 1)
    result, _ = verify(kh, revert_id)
    assert result["outcome"] == "healthy"
    status, revert, data = outcome(kh, revert_id)
    assert (status, revert, data["kind"]) == ("healthy", "not_requested", "revert")
    # A revert counts as a change: planning the same change again is a second reversal.
    assert code(kh.call(PLAN, {**target, "replicas": 2})) == "flapping"


def test_kill_switch_skips_the_revert_for_good(kh, best_effort):
    target = best_effort("it-verify-kill")
    plan_id = _apply_degrading(kh, target)
    kh.set_param(KILL_SWITCH, "false")
    result, _ = verify(kh, plan_id)
    assert (result["outcome"], result["revert"]) == ("degraded", "skipped")
    kh.set_param(KILL_SWITCH, "true")
    for _ in range(2):
        kh.make_due(plan_id)
        kh.run_verifier()
    status, revert, data = outcome(kh, plan_id)
    assert (status, revert) == ("degraded", "skipped")
    assert data["revert"]["reason"] == "remediation_disabled"
    assert kh.deployment(target["name"]).spec.replicas == 2


def test_degraded_change_without_revert_is_left(kh, best_effort):
    target = best_effort("it-verify-norevert")
    plan_id = _apply_degrading(kh, target, revert=False)
    result, _ = verify(kh, plan_id)
    assert result["outcome"] == "degraded" and result["revert"] is None
    status, revert, _ = outcome(kh, plan_id)
    assert (status, revert) == ("degraded", "not_requested")
    assert kh.deployment(target["name"]).spec.replicas == 2
