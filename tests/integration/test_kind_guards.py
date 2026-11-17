"""Namespace and replica bounds, GitOps ownership and HPAs, single use and expiry,
conflicts and the blast-radius budget (tests/e2e/e2e_gateway.py: validation_tests,
ownership_tests, plan_tests, the blast-radius case of impact_tests)."""

import threading

import pytest
from harness import NAMESPACE, code, deployment_manifest, details, unique

PLAN = "k8sr___plan_scale_deployment"
APPLY = "k8sw___apply_scale_deployment"
UNKNOWN_PLAN = "00000000-0000-4000-8000-000000000000"
WEB = {"namespace": NAMESPACE, "name": "web"}
CORRELATION_ANNOTATION = "kube-handrail.io/correlation-id"
LAST_PLAN_ANNOTATION = "kube-handrail.io/last-plan-id"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"namespace": "kube-system", "name": "coredns", "replicas": 2}, "forbidden_namespace"),
        ({**WEB, "replicas": 1000}, "out_of_bounds"),
        ({**WEB, "replicas": -1}, "out_of_bounds"),
        ({**WEB, "name": "web;kubectl delete ns x", "replicas": 2}, "invalid_argument"),
        ({"namespace": "../x", "name": "web", "replicas": 2}, "invalid_argument"),
        ({**WEB, "name": "does-not-exist", "replicas": 2}, "not_found"),
        ({**WEB, "replicas": "2"}, "invalid_argument"),
        ({**WEB, "replicas": 2.5}, "invalid_argument"),
        ({**WEB, "replicas": True}, "invalid_argument"),
    ],
)
def test_namespace_bounds_and_arguments(kh, arguments, expected):
    assert code(kh.call(PLAN, arguments)) == expected


def fixture(kh, prefix, replicas=1, **kwargs):
    return kh.create(deployment_manifest(unique(prefix), replicas, **kwargs))


def test_argo_annotation_is_gitops_managed(kh):
    target = fixture(
        kh,
        "it-argo",
        0,
        annotations={"argocd.argoproj.io/tracking-id": "it:apps/Deployment:ns/argo"},
    )
    assert code(kh.call(PLAN, {**target, "replicas": 1})) == "gitops_managed"


def test_flux_label_added_after_the_plan_is_caught_at_apply(kh):
    target = fixture(kh, "it-flux")
    plan_id = kh.plan(target, 2)
    kh.patch(target["name"], {"metadata": {"labels": {"kustomize.toolkit.fluxcd.io/name": "it"}}})
    assert code(kh.apply(target, plan_id, 2)) == "gitops_managed"


def test_helm_instance_label_counts_only_when_listed_in_gitops_markers(kh):
    target = fixture(kh, "it-helm", labels={"app.kubernetes.io/instance": "it"})
    assert kh.call(PLAN, {**target, "replicas": 2})["ok"] is True
    kh.configure(GITOPS_MARKERS='["app.kubernetes.io/instance"]')
    assert code(kh.call(PLAN, {**target, "replicas": 2})) == "gitops_managed"


def test_deployment_targeted_by_an_hpa_is_conflict(kh):
    from kubernetes import client as k8s

    target = fixture(kh, "it-hpa")
    hpa = {
        "apiVersion": "autoscaling/v2",
        "kind": "HorizontalPodAutoscaler",
        "metadata": {"name": target["name"], "namespace": NAMESPACE},
        "spec": {
            "scaleTargetRef": {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "name": target["name"],
            },
            "minReplicas": 1,
            "maxReplicas": 1,
        },
    }
    autoscaling = k8s.AutoscalingV2Api(kh.apps.api_client)
    autoscaling.create_namespaced_horizontal_pod_autoscaler(NAMESPACE, hpa)
    kh.created.append(("hpa", target["name"], NAMESPACE))
    envelope = kh.call(PLAN, {**target, "replicas": 2})
    # The refusal names the HPA, which plan_adjust_hpa changes instead (test_kind_hpa.py).
    assert code(envelope) == "conflict"
    assert details(envelope)["hpa"] == [target["name"]]


def test_plan_is_single_use_and_carries_correlation_ids(kh):
    target = fixture(kh, "it-single")
    assert code(kh.apply(target, UNKNOWN_PLAN, 2)) == "plan_not_found"
    planned = kh.call(PLAN, {**target, "replicas": 2})
    plan_id = planned["data"]["plan_id"]
    assert code(kh.apply(target, plan_id, 3)) == "plan_mismatch"
    # Label-only change: resourceVersion moves, generation and spec.replicas do not.
    kh.patch(target["name"], {"metadata": {"labels": {"kube-handrail.io/it-touch": "1"}}})
    applied = kh.apply(target, plan_id, 2)
    assert applied["ok"] is True, applied
    assert kh.deployment(target["name"]).spec.replicas == 2
    assert kh.annotation(target["name"], LAST_PLAN_ANNOTATION) == plan_id
    correlation = applied["data"]["correlation_id"]
    assert correlation and correlation != planned["data"]["correlation_id"]
    assert kh.annotation(target["name"], CORRELATION_ANNOTATION) == correlation
    assert code(kh.apply(target, plan_id, 2)) == "plan_used"


def test_expired_plan_is_refused(kh):
    target = fixture(kh, "it-expiry")
    plan_id = kh.plan(target, 2)
    ttl = int(kh.record(plan_id)["expires_at"] - kh.record(plan_id)["created_at"])
    kh.write_offset = ttl + 1
    assert code(kh.apply(target, plan_id, 2)) == "plan_expired"
    assert kh.record(plan_id)["used"] is False


def _conflict(envelope, changed):
    entry = details(envelope).get(changed) or {}
    planned = entry.get("planned", entry.get("planned_from"))
    return code(envelope) == "conflict" and entry.get("current") not in (None, planned)


def test_spec_change_between_plan_and_apply_is_conflict(kh):
    target = fixture(kh, "it-spec")
    plan_id = kh.plan(target, 2)
    container = {"name": "main", "env": [{"name": "IT_TOUCH", "value": "1"}]}
    kh.patch(target["name"], {"spec": {"template": {"spec": {"containers": [container]}}}})
    assert _conflict(kh.apply(target, plan_id, 2), "generation")


def test_manual_scale_between_plan_and_apply_is_conflict(kh):
    target = fixture(kh, "it-scale")
    plan_id = kh.plan(target, 2)
    kh.scale_by_hand(target["name"], 3)
    assert _conflict(kh.apply(target, plan_id, 2), "replicas")


def test_concurrent_applies_of_one_plan_exactly_one_succeeds(kh):
    target = fixture(kh, "it-concurrent")
    plan_id = kh.plan(target, 2)
    results = []
    barrier = threading.Barrier(2)

    def apply():
        barrier.wait()
        results.append(kh.apply(target, plan_id, 2))

    threads = [threading.Thread(target=apply) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # In-process threads do not always overlap: when the second apply starts after the first
    # has scaled the Deployment, its spec check refuses it with conflict before it reaches the
    # single-use condition. Either way exactly one apply succeeds; the live run keeps the
    # truly concurrent case on real DynamoDB.
    codes = sorted(code(r) or "ok" for r in results)
    assert codes in (["ok", "plan_used"], ["conflict", "ok"])


def test_step_beyond_the_blast_radius_is_refused(kh):
    target = fixture(kh, "it-blast")
    envelope = kh.call(PLAN, {**target, "replicas": 3})
    assert code(envelope) == "blast_radius_exceeded"
    assert details(envelope).get("max_step") == 1
