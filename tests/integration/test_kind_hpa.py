"""HPA adjust (mcp/contract/adjust-hpa.md) on kind with metrics-server: a minimum raised from
1 to 2 and verified healthy; a minimum the quota cannot fit, verified degraded and reverted
with revert_on_failure; the writer refused by the HPA admission policy when it changes
scaleTargetRef; and a scale plan on an HPA-targeted Deployment pointing at plan_adjust_hpa.
As in test_kind_verify.py, a case moves verify_at into the past instead of waiting."""

import json

import pytest
from harness import (
    NAMESPACE,
    NAMESPACE_2,
    QUOTA,
    code,
    deployment_manifest,
    details,
    hpa_manifest,
    kubectl,
    unique,
    wait_until,
)
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

PLAN = "k8sr___plan_adjust_hpa"
APPLY = "k8sw___apply_adjust_hpa"
OUTCOME = "k8sr___get_plan_outcome"
LAST_PLAN_ANNOTATION = "kube-handrail.io/last-plan-id"
HPA_POLICY = "kube-handrail-writer-hpa-bounds"
WINDOW = 60


def verify(kh, plan_id):
    kh.make_due(plan_id)
    run = kh.run_verifier()
    results = {r["plan_id"]: r for r in run["results"]}
    assert plan_id in results, run
    return results[plan_id]


def condition(obj, kind):
    return next((c for c in (obj.status.conditions or []) if c.type == kind), None)


def status_of(obj, kind):
    found = condition(obj, kind)
    return None if found is None else found.status


def autoscaled(kh, prefix, best_effort=False):
    """A one-replica Deployment, available, under an HPA with bounds 1 to 3 that the
    controller has seen (AbleToScale reported)."""
    target = kh.create(deployment_manifest(unique(prefix), best_effort=best_effort))
    kh.wait_available(target["name"], 1)
    hpa = kh.create_hpa(hpa_manifest(f"{target['name']}-hpa", target["name"]))
    wait_until(
        "the HPA controller to report AbleToScale",
        lambda: condition(kh.hpa(hpa["name"]), "AbleToScale") is not None,
        timeout=90,
    )
    return target, hpa


def test_a_raised_minimum_is_applied_and_verified_healthy(kh):
    target, hpa = autoscaled(kh, "it-hpa-ok")
    arguments = {**hpa, "min_replicas": 2, "verify_window_seconds": WINDOW}
    planned = kh.call(PLAN, arguments)
    assert planned["ok"], planned
    data = planned["data"]
    assert data["diff"] == {
        "hpa": {
            "min_replicas": {"from": 1, "to": 2},
            "max_replicas": {"from": 3, "to": 3},
            "scale_target": target["name"],
        },
        "replicas": {"from": 1, "to": 2},
    }
    assert data["impact"]["pods"]["added"] == 1
    record = kh.record(data["plan_id"])
    assert record["target_kind"] == "HorizontalPodAutoscaler"
    assert record["generation"] == -1

    assert code(kh.call(APPLY, {"plan_id": data["plan_id"], **hpa, "min_replicas": 3})) == (
        "plan_mismatch"
    )
    applied = kh.call(APPLY, {"plan_id": data["plan_id"], **arguments})
    assert applied["ok"], applied
    live = kh.hpa(hpa["name"])
    assert (live.spec.min_replicas, live.spec.max_replicas) == (2, 3)
    assert live.metadata.annotations[LAST_PLAN_ANNOTATION] == data["plan_id"]
    # The HPA controller scales the Deployment to the new minimum.
    kh.wait_available(target["name"], 2, timeout=120)
    wait_until(
        "the HPA to report 2 current replicas",
        lambda: kh.hpa(hpa["name"]).status.current_replicas == 2,
        timeout=60,
    )
    result = verify(kh, data["plan_id"])
    assert (result["outcome"], result["reason"]) == ("healthy", "target_reached"), result
    outcome = kh.call(OUTCOME, {"plan_id": data["plan_id"]})["data"]
    assert outcome["target_kind"] == "HorizontalPodAutoscaler"
    assert outcome["hpa"]["min_replicas"] == {"from": 1, "to": 2}
    assert outcome["verification"]["observed"]["target"]["available"] == 2


def _quota_used(kh):
    quota = kh.core.read_namespaced_resource_quota(QUOTA, NAMESPACE)
    return int(((quota.status and quota.status.used) or {}).get("pods", "0"))


def test_a_minimum_the_quota_cannot_fit_is_reverted(kh):
    wait_until("the BestEffort quota to be free", lambda: _quota_used(kh) == 0, timeout=90)
    # BestEffort pods have no CPU request, so the HPA has no utilization to compute.
    target, hpa = autoscaled(kh, "it-hpa-quota", best_effort=True)
    wait_until(
        "the HPA to report its metrics unavailable",
        lambda: status_of(kh.hpa(hpa["name"]), "ScalingActive") == "False",
        timeout=90,
    )
    arguments = {
        **hpa,
        "min_replicas": 2,
        "verify_window_seconds": WINDOW,
        "revert_on_failure": True,
    }
    planned = kh.call(PLAN, arguments)
    assert planned["ok"], planned
    data = planned["data"]
    assert [w["code"] for w in data["impact"]["warnings"]] == ["hpa_metrics_unavailable"]
    assert data["verification"]["revert_to"] == {"min_replicas": 1, "max_replicas": 3}
    # Applied 30 s ago, so the revert lands after it in the history.
    kh.write_offset = -30
    assert kh.call(APPLY, {"plan_id": data["plan_id"], **arguments})["ok"]
    kh.write_offset = 0
    # The controller applies the minimum without metrics; the quota refuses the second pod.
    wait_until(
        "the quota to refuse the second pod",
        lambda: (
            kh.deployment(target["name"]).spec.replicas == 2
            and status_of(kh.deployment(target["name"]), "ReplicaFailure") == "True"
        ),
        timeout=90,
    )
    result = verify(kh, data["plan_id"])
    assert result["outcome"] == "degraded", result
    assert result["reason"] in ("replica_failure", "target_not_reached")
    assert result["revert"] == "reverted"
    live = kh.hpa(hpa["name"])
    assert (live.spec.min_replicas, live.spec.max_replicas) == (1, 3)
    record = kh.record(data["plan_id"])
    revert_id = record["revert"]["revert_plan_id"]
    assert live.metadata.annotations[LAST_PLAN_ANNOTATION] == revert_id
    revert = kh.record(revert_id)
    assert (revert["action"], revert["target_kind"], revert["kind"]) == (
        "adjust_hpa",
        "HorizontalPodAutoscaler",
        "revert",
    )
    assert (int(revert["min_to"]), int(revert["max_to"])) == (1, 3)
    # The revert restores the bounds, not the pods: without metrics the controller does not
    # scale down within them (mcp/contract/adjust-hpa.md, Findings on kind).
    assert kh.deployment(target["name"]).spec.replicas == 2
    # The revert record is verified against the restored bounds: still two desired pods,
    # one refused, so it is degraded too, and never reverted itself.
    result = verify(kh, revert_id)
    assert (result["outcome"], result["reason"], result["revert"]) == (
        "degraded",
        "replica_failure",
        None,
    )


def test_the_writer_cannot_retarget_an_hpa(kh):
    target, hpa = autoscaled(kh, "it-hpa-frozen")
    writer = k8s.AutoscalingV2Api(kh.writer)
    body = {"spec": {"scaleTargetRef": {"name": "kh-int-other"}}}
    with pytest.raises(ApiException) as error:
        writer.patch_namespaced_horizontal_pod_autoscaler(hpa["name"], NAMESPACE, body)
    assert error.value.status == 422
    assert f"ValidatingAdmissionPolicy '{HPA_POLICY}'" in error.value.body
    with pytest.raises(ApiException) as error:
        writer.patch_namespaced_horizontal_pod_autoscaler(
            hpa["name"], NAMESPACE, {"spec": {"maxReplicas": 6}}
        )
    assert error.value.status == 422 and HPA_POLICY in error.value.body
    # Within the bounds, the writer may change them (what apply_adjust_hpa does).
    writer.patch_namespaced_horizontal_pod_autoscaler(
        hpa["name"], NAMESPACE, {"spec": {"maxReplicas": 4}}, dry_run="All"
    )
    # No HPA rights where adjust_hpa is not allowed.
    with pytest.raises(ApiException) as error:
        writer.read_namespaced_horizontal_pod_autoscaler("kh-int-none", NAMESPACE_2)
    assert error.value.status == 403


def test_a_scale_plan_on_an_autoscaled_deployment_names_the_hpa(kh):
    target, hpa = autoscaled(kh, "it-hpa-hint")
    envelope = kh.call("k8sr___plan_scale_deployment", {**target, "replicas": 2})
    assert code(envelope) == "conflict"
    assert details(envelope) == {
        "hpa": [hpa["name"]],
        "hint": f"use plan_adjust_hpa on {hpa['name']}",
    }


def test_adjust_hpa_refusals(kh):
    target, hpa = autoscaled(kh, "it-hpa-refused")
    assert code(kh.call(PLAN, {**hpa, "max_replicas": 6})) == "out_of_bounds"
    assert code(kh.call(PLAN, {**hpa, "min_replicas": 3, "max_replicas": 2})) == (
        "invalid_argument"
    )
    refused = kh.call(PLAN, {"namespace": NAMESPACE_2, "name": hpa["name"], "min_replicas": 2})
    assert code(refused) == "forbidden_namespace"
    assert details(refused)["action"] == "adjust_hpa"
    assert code(kh.call(PLAN, {**hpa, "name": "kh-int-none", "min_replicas": 2})) == "not_found"


def test_an_exact_match_policy_refuses_hpa_plans(kh):
    """matchPolicy Exact would let the writer change an HPA through autoscaling/v1 unchecked,
    so the plan-time check refuses it like any other edit of the policy."""
    target, hpa = autoscaled(kh, "it-hpa-exact")

    def match_policy(value):
        kubectl(
            "patch",
            "validatingadmissionpolicy",
            HPA_POLICY,
            "--type",
            "merge",
            "-p",
            json.dumps({"spec": {"matchConstraints": {"matchPolicy": value}}}),
        )

    match_policy("Exact")
    try:
        envelope = kh.call(PLAN, {**hpa, "min_replicas": 2})
        assert code(envelope) == "conflict", envelope
        assert details(envelope)["reason"] == "policy_modified"
        assert details(envelope)["policy"] == HPA_POLICY
    finally:
        # The rendered policy leaves matchPolicy to its default, so applying it again would
        # keep Exact.
        match_policy("Equivalent")
    assert kh.call(PLAN, {**hpa, "min_replicas": 2})["ok"] is True
