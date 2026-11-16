"""The namespace policy (Milestone 6) on the rendered manifests: actions per namespace,
per-namespace approvers, the plan-time check of the admission binding and the plan's
cluster (tests/e2e/e2e_gateway.py: namespace_tests)."""

import pytest
from harness import (
    APPROVER,
    CLUSTER,
    NAMESPACE,
    NAMESPACE_2,
    NAMESPACE_2_APPROVER,
    code,
    deployment_manifest,
    details,
    set_binding_actions,
    unique,
)

PLAN_SCALE = "k8sr___plan_scale_deployment"
APPLY_SCALE = "k8sw___apply_scale_deployment"
PLAN_RESTART = "k8sr___plan_restart_deployment"
APPLY_RESTART = "k8sw___apply_restart_deployment"
PLAN_ROLLBACK = "k8sr___plan_rollback_deployment"
APPLY_ROLLBACK = "k8sw___apply_rollback_deployment"
REQUEST_APPROVAL = "k8sr___request_approval"
OUTCOME = "k8sr___get_plan_outcome"
RESTART_ANNOTATION = "kube-handrail.io/restart-plan-id"
UNKNOWN_PLAN = "00000000-0000-4000-8000-000000000000"


def restart_only(kh, prefix):
    return kh.create(deployment_manifest(unique(prefix), namespace=NAMESPACE_2))


@pytest.mark.parametrize(
    ("tool", "extra", "action"),
    [
        (PLAN_SCALE, {"replicas": 2}, "scale"),
        (PLAN_ROLLBACK, {"revision": 1}, "rollback"),
        (APPLY_SCALE, {"replicas": 2, "plan_id": UNKNOWN_PLAN}, "scale"),
        (APPLY_ROLLBACK, {"revision": 1, "plan_id": UNKNOWN_PLAN}, "rollback"),
    ],
)
def test_action_the_namespace_does_not_allow_is_forbidden(kh, tool, extra, action):
    target = restart_only(kh, "it-ns-forbidden")
    envelope = kh.call(tool, {**target, **extra})
    assert code(envelope) == "forbidden_namespace", envelope
    assert details(envelope) == {"action": action, "allowed_actions": ["restart"]}
    assert kh.deployment(target["name"], NAMESPACE_2).spec.replicas == 1


def test_restart_in_the_restart_only_namespace(kh):
    """Planned and applied through the rendered writer Role of the second namespace."""
    target = restart_only(kh, "it-ns-restart")
    planned = kh.call(PLAN_RESTART, target)
    assert planned["ok"] is True, planned
    plan_id = planned["data"]["plan_id"]
    envelope = kh.call(APPLY_RESTART, {"plan_id": plan_id, **target})
    assert envelope["ok"] is True, envelope
    template = kh.deployment(target["name"], NAMESPACE_2).spec.template.metadata.annotations
    assert template and template.get(RESTART_ANNOTATION) == plan_id


def decide(kh, plan_id, caller, decision="approve"):
    return kh.approver("POST", caller, body={"plan_id": plan_id, "decision": decision})


def test_namespace_approvers_decide_only_their_namespace(kh):
    kh.configure(APPROVAL_MODE="server")
    target = restart_only(kh, "it-ns-approver")
    plan_id = kh.call(PLAN_RESTART, target)["data"]["plan_id"]
    assert kh.call(REQUEST_APPROVAL, {"plan_id": plan_id})["ok"] is True
    refusal = {"reason": "not_namespace_approver", "namespace": NAMESPACE_2}

    # A global approver is not an approver of a namespace that lists its own.
    status, envelope = kh.approver("GET", APPROVER, plan_id=plan_id)
    assert (status, code(envelope), details(envelope)) == (403, "forbidden", refusal)
    status, envelope = decide(kh, plan_id, APPROVER)
    assert (status, code(envelope), details(envelope)) == (403, "forbidden", refusal)
    assert kh.record(plan_id)["status"] == "awaiting_approval"

    # Nor is the namespace's approver one of the sandbox, which uses the global list.
    sandbox = kh.create(deployment_manifest(unique("it-ns-global")))
    sandbox_plan = kh.plan(sandbox, 2)
    status, envelope = decide(kh, sandbox_plan, NAMESPACE_2_APPROVER)
    assert (status, code(envelope)) == (403, "forbidden")
    assert details(envelope) == {"reason": "not_namespace_approver", "namespace": NAMESPACE}

    status, envelope = kh.approver("GET", NAMESPACE_2_APPROVER, plan_id=plan_id)
    assert status == 200 and envelope["data"]["cluster"] == CLUSTER
    status, envelope = decide(kh, plan_id, NAMESPACE_2_APPROVER)
    assert status == 200 and envelope["data"]["apply"]["ok"] is True, envelope
    assert kh.record(plan_id)["approved_by"] == NAMESPACE_2_APPROVER


def test_binding_switched_to_warn_refuses_every_plan(kh):
    """The read Lambda reads the binding on every plan: in Warn the cluster would only warn,
    so nothing is planned (conflict, binding_not_deny) until it denies again."""
    sandbox = kh.create(deployment_manifest(unique("it-ns-warn")))
    second = restart_only(kh, "it-ns-warn")
    set_binding_actions("Warn", "Audit")
    try:
        for tool, target, extra in (
            (PLAN_SCALE, sandbox, {"replicas": 2}),
            (PLAN_RESTART, second, {}),
        ):
            envelope = kh.call(tool, {**target, **extra})
            assert code(envelope) == "conflict", envelope
            assert details(envelope)["check"] == "admission_policy_match"
            assert details(envelope)["reason"] == "binding_not_deny"
            assert details(envelope)["validation_actions"] == ["Warn", "Audit"]
    finally:
        kh.restore_policy()
    assert kh.call(PLAN_SCALE, {**sandbox, "replicas": 2})["ok"] is True


def test_admission_check_off_plans_without_reading_the_policy(kh):
    """Only ADMISSION_CHECK=false skips the check; the plan says so in its preflight."""
    target = kh.create(deployment_manifest(unique("it-ns-check-off")))
    set_binding_actions("Warn", "Audit")
    try:
        kh.configure(ADMISSION_CHECK="false")
        envelope = kh.call(PLAN_SCALE, {**target, "replicas": 2})
        assert envelope["ok"] is True, envelope
        entry = next(
            c for c in envelope["data"]["preflight"] if c["check"] == "admission_policy_match"
        )
        assert entry == {"check": "admission_policy_match", "passed": True, "skipped": True}
        kh.configure()
        assert code(kh.call(PLAN_SCALE, {**target, "replicas": 2})) == "conflict"
    finally:
        kh.restore_policy()


def test_plan_records_its_cluster(kh):
    target = kh.create(deployment_manifest(unique("it-ns-cluster")))
    planned = kh.call(PLAN_SCALE, {**target, "replicas": 2})
    checks = {c["check"]: c for c in planned["data"]["preflight"]}
    for check in ("action_allowed", "admission_policy_match"):
        assert checks[check] == {"check": check, "passed": True}
    plan_id = planned["data"]["plan_id"]
    assert kh.record(plan_id)["cluster"] == CLUSTER
    assert kh.apply(target, plan_id, 2)["ok"] is True
    envelope = kh.call(OUTCOME, {"plan_id": plan_id})
    assert envelope["ok"] is True and envelope["data"]["cluster"] == CLUSTER, envelope
