"""Restart and rollback, and the cluster's admission policy behind the write Lambda
(tests/e2e/e2e_gateway.py: rollout_tests, rollback_refusal_tests, rollback_tests; the
admission policy's own cases are tests/admission/run.sh)."""

import json

import harness
import pytest
from harness import (
    IMAGE,
    KILL_SWITCH,
    MAX_REPLICAS,
    MIN_REPLICAS,
    NAMESPACE,
    code,
    deployment_manifest,
    details,
    kubectl,
    unique,
    wait_until,
)
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

PLAN_RESTART = "k8sr___plan_restart_deployment"
APPLY_RESTART = "k8sw___apply_restart_deployment"
HISTORY = "k8sr___get_rollout_history"
PLAN_ROLLBACK = "k8sr___plan_rollback_deployment"
APPLY_ROLLBACK = "k8sw___apply_rollback_deployment"
RESTART_ANNOTATION = "kube-handrail.io/restart-plan-id"
LAST_PLAN_ANNOTATION = "kube-handrail.io/last-plan-id"
REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
# Never pulled: the rollback fixture runs no pods.
OLD_IMAGE = "registry.k8s.io/pause:3.9"
OPEN, CLOSE = "<untrusted>", "</untrusted>"


def pods(kh, name):
    """Pod name => restart annotation of the fixture's pods that are not being deleted."""
    items = kh.core.list_namespaced_pod(
        NAMESPACE, label_selector=f"app.kubernetes.io/name={name}"
    ).items
    return {
        p.metadata.name: (p.metadata.annotations or {}).get(RESTART_ANNOTATION, "")
        for p in items
        if p.metadata.deletion_timestamp is None
    }


def test_restart(kh):
    kh.configure(APPLY_COOLDOWN_SECONDS="60")
    target = kh.create(deployment_manifest(unique("it-restart"), 2, surge_all=True))
    kh.wait_available(target["name"], 2)
    before = pods(kh, target["name"])
    planned = kh.call(PLAN_RESTART, target)
    assert planned["ok"] is True, planned
    data = planned["data"]
    assert data["diff"]["restart"] == {"pods_replaced": 2}
    assert data["impact"]["rollout"]["strategy"] == "RollingUpdate"
    assert data["impact"]["pods"]["replaced"] == 2
    plan_id = data["plan_id"]
    assert kh.call(APPLY_RESTART, {"plan_id": plan_id, **target})["ok"] is True
    assert code(kh.call(PLAN_RESTART, target)) == "cooldown"

    deployment = kh.wait_available(target["name"], 2)
    assert deployment.spec.replicas == 2
    assert deployment.spec.template.metadata.annotations[RESTART_ANNOTATION] == plan_id
    after = wait_until(
        "the old pods to go",
        lambda: (p := pods(kh, target["name"])) and len(p) == 2 and not set(p) & set(before) and p,
    )
    assert set(after.values()) == {plan_id}

    # After the cooldown (the restart moved into the past), the kill switch still applies.
    other = kh.create(deployment_manifest(unique("it-restart-kill"), 0))
    first = kh.call(PLAN_RESTART, other)["data"]["plan_id"]
    kh.write_offset = -61
    assert kh.call(APPLY_RESTART, {"plan_id": first, **other})["ok"] is True
    kh.write_offset = 0
    second = kh.call(PLAN_RESTART, other)
    assert second["ok"] is True, second
    kh.set_param(KILL_SWITCH, "false")
    envelope = kh.call(APPLY_RESTART, {"plan_id": second["data"]["plan_id"], **other})
    assert code(envelope) == "remediation_disabled"


def roll_forward(kh, name, image, value):
    container = {"name": "main", "image": image, "env": [{"name": "IT_REVISION", "value": value}]}
    kh.patch(name, {"spec": {"template": {"spec": {"containers": [container]}}}})
    kh.wait_observed(name)


def replica_sets(kh, name):
    """Revision => ReplicaSet of the fixture."""
    items = (
        k8s.AppsV1Api(kh.apps.api_client)
        .list_namespaced_replica_set(NAMESPACE, label_selector=f"app.kubernetes.io/name={name}")
        .items
    )
    return {int(rs.metadata.annotations[REVISION_ANNOTATION]): rs for rs in items}


def template_without_hash(kh, rs_name):
    raw = json.loads(kubectl("get", "rs", rs_name, "-o", "json"))["spec"]["template"]
    labels = raw.get("metadata", {}).get("labels", {})
    labels.pop("pod-template-hash", None)
    return raw


def deployment_template(name):
    return json.loads(kubectl("get", f"deploy/{name}", "-o", "json"))["spec"]["template"]


@pytest.fixture
def rollback_target(kh):
    """Zero replicas, so no pod is ever pulled; revisionHistoryLimit 3 and rolled forward to
    revision 5, so revision 1 is pruned, as the live fixture e2e-rollback."""
    target = kh.create(deployment_manifest(unique("it-rollback"), 0, revision_history_limit=3))
    kh.wait_observed(target["name"])
    for image, value in ((OLD_IMAGE, "2"), (OLD_IMAGE, "3"), (OLD_IMAGE, "4"), (IMAGE, "5")):
        roll_forward(kh, target["name"], image, value)
    wait_until(
        "revision 1 to be pruned", lambda: sorted(replica_sets(kh, target["name"])) == [2, 3, 4, 5]
    )
    return target


def test_rollback_refusals(kh, rollback_target):
    target = rollback_target
    revisions = kh.call(HISTORY, target)["data"]["revisions"]
    assert [(r["revision"], r["current"]) for r in revisions] == [
        (5, True),
        (4, False),
        (3, False),
        (2, False),
    ]
    assert [i["image"] for i in revisions[2]["images"]] == [f"{OPEN}{OLD_IMAGE}{CLOSE}"]

    kh.patch(target["name"], {"spec": {"paused": True}})
    envelope = kh.call(PLAN_ROLLBACK, {**target, "revision": 3})
    assert code(envelope) == "conflict" and details(envelope) == {"paused": True}
    kh.patch(target["name"], {"spec": {"paused": False}})

    envelope = kh.call(PLAN_ROLLBACK, {**target, "revision": 1})
    assert code(envelope) == "revision_not_found"
    assert details(envelope) == {"revision": 1, "available": [5, 4, 3, 2]}
    assert code(kh.call(PLAN_ROLLBACK, {**target, "revision": 5})) == "invalid_argument"

    gitops = kh.create(
        deployment_manifest(
            unique("it-rollback-argo"),
            0,
            annotations={"argocd.argoproj.io/tracking-id": "it:apps/Deployment:ns/argo"},
        )
    )
    assert code(kh.call(PLAN_ROLLBACK, {**gitops, "revision": 1})) == "gitops_managed"

    # The pinned ReplicaSet's template changes between plan and apply; the Deployment's
    # generation does not.
    planned = kh.call(PLAN_ROLLBACK, {**target, "revision": 2})
    rs = planned["data"]["diff"]["replica_set"]
    touch = {"spec": {"template": {"metadata": {"annotations": {"kube-handrail.io/it": "1"}}}}}
    k8s.AppsV1Api(kh.apps.api_client).patch_namespaced_replica_set(rs, NAMESPACE, touch)
    envelope = kh.call(
        APPLY_ROLLBACK, {"plan_id": planned["data"]["plan_id"], **target, "revision": 2}
    )
    assert code(envelope) == "conflict" and "template" in details(envelope)


def test_rollback_and_manual_change(kh, rollback_target):
    target = rollback_target
    planned = kh.call(PLAN_ROLLBACK, {**target, "revision": 3})
    assert planned["ok"] is True, planned
    diff = planned["data"]["diff"]
    assert diff["revision"] == {"from": 5, "to": 3}
    assert [(i["from"], i["to"], i["changed"]) for i in diff["images"]] == [
        (f"{OPEN}{IMAGE}{CLOSE}", f"{OPEN}{OLD_IMAGE}{CLOSE}", True)
    ]
    assert diff["fields_changed"] == ["spec.containers[main].env", "spec.containers[main].image"]
    plan_id, rs = planned["data"]["plan_id"], diff["replica_set"]
    envelope = kh.call(APPLY_ROLLBACK, {"plan_id": plan_id, **target, "revision": 4})
    assert code(envelope) == "plan_mismatch"
    applied = kh.call(APPLY_ROLLBACK, {"plan_id": plan_id, **target, "revision": 3})
    assert applied["ok"] is True, applied
    assert deployment_template(target["name"]) == template_without_hash(kh, rs)
    assert kh.annotation(target["name"], LAST_PLAN_ANNOTATION) == plan_id
    wait_until(
        "the target ReplicaSet to be renumbered", lambda: 6 in replica_sets(kh, target["name"])
    )
    assert replica_sets(kh, target["name"])[6].metadata.name == rs

    # A deploy by hand on top of the rollback: rolling back again would undo it.
    roll_forward(kh, target["name"], IMAGE, "3")
    envelope = kh.call(PLAN_ROLLBACK, {**target, "revision": 4})
    assert code(envelope) == "manual_change"
    assert details(envelope)["last_applied_template"] == diff["template"]["to"]
    assert details(envelope)["current_template"] not in (None, diff["template"]["to"])
    planned = kh.call(PLAN_ROLLBACK, {**target, "revision": 4, "override_manual_change": True})
    assert planned["ok"] is True, planned
    envelope = kh.call(
        APPLY_ROLLBACK,
        {
            "plan_id": planned["data"]["plan_id"],
            **target,
            "revision": 4,
            "override_manual_change": True,
        },
    )
    assert envelope["ok"] is True, envelope


def _apply_policy(min_replicas, max_replicas):
    """The deploy sed, then the binding back in Deny (the file commits Warn and Audit)."""
    kubectl("apply", "-f", "-", stdin=harness.admission_policy(min_replicas, max_replicas))
    kubectl(
        "patch",
        "validatingadmissionpolicybinding",
        "kube-handrail-writer-bounds",
        "--type",
        "merge",
        "-p",
        '{"spec":{"validationActions":["Deny"]}}',
    )


def _writer_dry_run_denied(kh, name, replicas):
    try:
        k8s.AppsV1Api(kh.writer).patch_namespaced_deployment(
            name, NAMESPACE, {"spec": {"replicas": replicas}}, dry_run="All"
        )
    except ApiException as exc:
        return exc.status == 422 and "kube-handrail-writer-bounds" in (exc.body or "")
    return False


def test_admission_policy_refuses_what_the_server_bounds_allow(kh):
    """The policy's bounds drifted below the Lambdas' (MAX_REPLICAS 5, the policy 3): the
    server's checks pass, the API server refuses the writer's patch, and the plan is used."""
    target = kh.create(deployment_manifest(unique("it-admission"), 2))
    _apply_policy(MIN_REPLICAS, 3)
    try:
        wait_until("the narrower policy", lambda: _writer_dry_run_denied(kh, target["name"], 4))
        plan_id = kh.plan(target, 4)
        envelope = kh.apply(target, plan_id, 4)
        assert code(envelope) == "admission_denied"
        assert details(envelope)["policy"] == "kube-handrail-writer-bounds"
        assert kh.record(plan_id)["used"] is True
        assert kh.deployment(target["name"]).spec.replicas == 2
        # Within the policy's bounds the writer is admitted.
        assert kh.apply(target, kh.plan(target, 3), 3)["ok"] is True
    finally:
        _apply_policy(MIN_REPLICAS, MAX_REPLICAS)
        wait_until(
            "the policy with the Lambdas' bounds",
            lambda: not _writer_dry_run_denied(kh, target["name"], 4),
        )
