import dataclasses
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest
from kubernetes import client as k8s
from kubernetes.client.rest import ApiException
from moto import mock_aws

from handrail_common.config import Settings
from handrail_common.breaker import LAST_PLAN_ANNOTATION, retention_seconds
from handrail_common.plans import PlanStore, object_key, scale_args_hash
from handrail_common.validation import GuardError, require_plan_id

HANDLER_PATH = Path(__file__).resolve().parents[2] / "mcp" / "k8s_read" / "handler.py"
NOW = 1_700_000_000
TABLE = "plans-test"
REGION = "us-east-1"


def _load_handler():
    # Both Lambdas ship a module named `handler`; load this one under a unique name.
    spec = importlib.util.spec_from_file_location("k8s_read_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


handler = _load_handler()

SETTINGS = Settings(
    cluster_name="test-cluster",
    plans_table=TABLE,
    allowed_namespaces=frozenset({"sandbox"}),
    min_replicas=0,
    max_replicas=5,
    plan_ttl_seconds=300,
    remediation_enabled_param=None,
    region=REGION,
)


def deployment(
    replicas: int | None = 2,
    annotations: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
) -> k8s.V1Deployment:
    return k8s.V1Deployment(
        metadata=k8s.V1ObjectMeta(
            namespace="sandbox",
            name="web",
            resource_version="4242",
            generation=7,
            uid="8f7c1a2e-0000-4000-8000-000000000001",
            annotations={"deployment.kubernetes.io/revision": "7", **(annotations or {})},
            labels=labels,
        ),
        spec=k8s.V1DeploymentSpec(
            replicas=replicas,
            selector=k8s.V1LabelSelector(match_labels={"app": "web"}),
            template=k8s.V1PodTemplateSpec(),
        ),
        status=k8s.V1DeploymentStatus(
            ready_replicas=2,
            updated_replicas=2,
            available_replicas=1,
            conditions=[
                k8s.V1DeploymentCondition(
                    type="Available", status="True", reason="MinimumReplicasAvailable"
                )
            ],
        ),
    )


def hpa(target_name: str, kind: str = "Deployment") -> k8s.V2HorizontalPodAutoscaler:
    return k8s.V2HorizontalPodAutoscaler(
        metadata=k8s.V1ObjectMeta(name=f"{target_name}-hpa"),
        spec=k8s.V2HorizontalPodAutoscalerSpec(
            max_replicas=5,
            scale_target_ref=k8s.V2CrossVersionObjectReference(
                api_version="apps/v1", kind=kind, name=target_name
            ),
        ),
    )


@pytest.fixture
def table(monkeypatch, plans_table):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        yield plans_table(TABLE, REGION)


def raw_list(items: list[dict]) -> SimpleNamespace:
    """A list response read with _preload_content=False, as the impact reads are."""
    return SimpleNamespace(data=json.dumps({"kind": "List", "items": items}).encode())


@pytest.fixture
def clients(table):
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = deployment()
    autoscaling = MagicMock()
    autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = (
        k8s.V2HorizontalPodAutoscalerList(items=[])
    )
    policy = MagicMock()
    policy.list_namespaced_pod_disruption_budget.return_value = raw_list([])
    core = MagicMock()
    core.list_namespaced_service.return_value = raw_list([])
    discovery = MagicMock()
    discovery.list_namespaced_endpoint_slice.return_value = raw_list([])
    return SimpleNamespace(
        apps=apps,
        autoscaling=autoscaling,
        policy=policy,
        core=core,
        discovery=discovery,
        store=PlanStore(table),
    )


def plan(clients, **overrides):
    args = {"namespace": "sandbox", "name": "web", "replicas": 3, **overrides}
    return handler.plan_scale_deployment(args, SETTINGS, clients, NOW)


def plan_error(clients, **overrides) -> str:
    with pytest.raises(GuardError) as exc:
        plan(clients, **overrides)
    return exc.value.code


def test_plan_stores_record_and_returns_diff(clients, table):
    result = plan(clients)

    assert result["ok"] is True
    data = result["data"]
    assert require_plan_id(data["plan_id"]) == data["plan_id"]
    assert data["diff"] == {"replicas": {"from": 2, "to": 3}}
    assert data["expires_at"] == NOW + 300
    assert all(check["passed"] for check in data["preflight"])

    item = table.get_item(Key={"plan_id": data["plan_id"]})["Item"]
    assert item["action"] == "scale_deployment"
    assert item["args_hash"] == scale_args_hash("sandbox", "web", 3)
    assert item["resource_version"] == "4242"
    assert item["generation"] == 7
    assert item["uid"] == "8f7c1a2e-0000-4000-8000-000000000001"
    assert (item["replicas_from"], item["replicas_to"]) == (2, 3)
    assert (item["created_at"], item["expires_at"]) == (NOW, NOW + 300)
    assert item["used"] is False
    assert item["object_key"] == "sandbox/web/8f7c1a2e-0000-4000-8000-000000000001"
    assert item["retain_until"] == NOW + retention_seconds(SETTINGS)
    assert data["override_manual_change"] is False
    assert [check["check"] for check in data["preflight"]][-6:] == [
        "no_manual_change",
        "cooldown",
        "not_flapping",
        "object_budget",
        "plan_rate",
        "global_budget",
    ]


def test_plan_defaults_missing_replicas_to_one(clients):
    clients.apps.read_namespaced_deployment.return_value = deployment(replicas=None)
    assert plan(clients, replicas=2)["data"]["diff"]["replicas"]["from"] == 1


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"namespace": "Bad_NS"}, "invalid_argument"),
        ({"name": "web/x"}, "invalid_argument"),
        ({"replicas": "3"}, "invalid_argument"),
        ({"replicas": True}, "invalid_argument"),
        ({"namespace": "kube-system"}, "forbidden_namespace"),
        ({"replicas": 6}, "out_of_bounds"),
        ({"replicas": -1}, "out_of_bounds"),
    ],
)
def test_plan_rejects_bad_arguments_before_any_api_call(clients, table, overrides, code):
    assert plan_error(clients, **overrides) == code
    clients.apps.read_namespaced_deployment.assert_not_called()
    assert table.scan()["Count"] == 0


def test_plan_missing_deployment_is_not_found(clients):
    clients.apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    assert plan_error(clients) == "not_found"


def test_plan_other_api_error_is_kubernetes_error(clients):
    clients.apps.read_namespaced_deployment.side_effect = ApiException(
        status=403, reason="Forbidden"
    )
    with pytest.raises(GuardError) as exc:
        plan(clients)
    assert exc.value.code == "kubernetes_error"
    assert "403" in exc.value.message


def test_plan_hpa_list_error_is_kubernetes_error(clients):
    clients.autoscaling.list_namespaced_horizontal_pod_autoscaler.side_effect = ApiException(
        status=500, reason="Internal"
    )
    assert plan_error(clients) == "kubernetes_error"


@pytest.mark.parametrize(
    "meta",
    [
        {"annotations": {"argocd.argoproj.io/tracking-id": "app:apps/Deployment:sandbox/web"}},
        {"labels": {"app.kubernetes.io/instance": "web"}},
    ],
)
def test_plan_rejects_gitops_managed(clients, table, meta):
    clients.apps.read_namespaced_deployment.return_value = deployment(**meta)
    assert plan_error(clients) == "gitops_managed"
    assert table.scan()["Count"] == 0


def test_plan_rejects_deployment_targeted_by_hpa(clients, table):
    clients.autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = (
        k8s.V2HorizontalPodAutoscalerList(items=[hpa("other"), hpa("web")])
    )
    with pytest.raises(GuardError) as exc:
        plan(clients)
    assert exc.value.code == "conflict"
    assert "HorizontalPodAutoscaler" in exc.value.message
    assert table.scan()["Count"] == 0
    clients.autoscaling.list_namespaced_horizontal_pod_autoscaler.assert_called_once_with(
        namespace="sandbox"
    )


def test_plan_ignores_hpa_for_other_targets(clients):
    clients.autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = (
        k8s.V2HorizontalPodAutoscalerList(items=[hpa("other"), hpa("web", kind="StatefulSet")])
    )
    assert plan(clients)["ok"] is True


def test_plan_ids_are_unique(clients):
    assert plan(clients)["data"]["plan_id"] != plan(clients)["data"]["plan_id"]


def test_status_summary(clients):
    result = handler.get_workload_status({"namespace": "sandbox", "name": "web"}, SETTINGS, clients)

    assert result["ok"] is True
    assert result["data"] == {
        "namespace": "sandbox",
        "name": "web",
        "replicas": {"desired": 2, "ready": 2, "updated": 2, "available": 1},
        "conditions": [
            {
                "type": "Available",
                "status": "True",
                "reason": "MinimumReplicasAvailable",
                "message": None,
            }
        ],
        "revision": "7",
        "resource_version": "4242",
        "gitops_managed": False,
    }
    clients.apps.read_namespaced_deployment.assert_called_once_with(name="web", namespace="sandbox")


def test_status_reports_gitops_managed(clients):
    clients.apps.read_namespaced_deployment.return_value = deployment(
        labels={"app.kubernetes.io/instance": "web"}
    )
    result = handler.get_workload_status({"namespace": "sandbox", "name": "web"}, SETTINGS, clients)
    assert result["data"]["gitops_managed"] is True


def test_status_handles_empty_status(clients):
    dep = deployment()
    dep.status = None
    clients.apps.read_namespaced_deployment.return_value = dep
    data = handler.get_workload_status({"namespace": "sandbox", "name": "web"}, SETTINGS, clients)[
        "data"
    ]
    assert data["replicas"] == {"desired": 2, "ready": 0, "updated": 0, "available": 0}
    assert data["conditions"] == []


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"namespace": "sandbox"}, "invalid_argument"),
        ({"namespace": "kube-system", "name": "web"}, "forbidden_namespace"),
    ],
)
def test_status_rejects_bad_arguments(clients, args, code):
    with pytest.raises(GuardError) as exc:
        handler.get_workload_status(args, SETTINGS, clients)
    assert exc.value.code == code
    clients.apps.read_namespaced_deployment.assert_not_called()


def context(tool: str) -> SimpleNamespace:
    return SimpleNamespace(
        client_context=SimpleNamespace(custom={"bedrockAgentCoreToolName": f"k8sr___{tool}"})
    )


@pytest.fixture
def wired(monkeypatch, clients):
    monkeypatch.setattr(handler, "_settings", lambda: SETTINGS)
    monkeypatch.setattr(handler, "Clients", lambda settings: clients)
    return clients


def test_lambda_handler_plans_through_dispatch(wired):
    event = {"namespace": "sandbox", "name": "web", "replicas": 3}
    result = handler.lambda_handler(event, context("plan_scale_deployment"))
    assert result["ok"] is True
    assert result["data"]["diff"] == {"replicas": {"from": 2, "to": 3}}


def test_lambda_handler_maps_guard_errors(wired):
    wired.apps.read_namespaced_deployment.side_effect = ApiException(status=404)
    result = handler.lambda_handler(
        {"namespace": "sandbox", "name": "web"}, context("get_workload_status")
    )
    assert result["error"]["code"] == "not_found"


def test_lambda_handler_unknown_tool(wired):
    result = handler.lambda_handler({}, context("apply_scale_deployment"))
    assert result["error"]["code"] == "unknown_tool"


def test_lambda_handler_missing_config_is_internal(monkeypatch):
    handler._settings.cache_clear()
    for key in ("CLUSTER_NAME", "PLANS_TABLE", "ALLOWED_NAMESPACES"):
        monkeypatch.delenv(key, raising=False)
    result = handler.lambda_handler(
        {"namespace": "sandbox", "name": "web"}, context("get_workload_status")
    )
    handler._settings.cache_clear()
    assert result["error"] == {"code": "internal", "message": "internal error"}


# --- Oscillation breaker ----------------------------------------------------

UID = "8f7c1a2e-0000-4000-8000-000000000001"
KEY = object_key("sandbox", "web", UID)


def seed(
    table,
    replicas_from: int,
    replicas_to: int,
    created_at: int,
    applied_at: int | None = None,
    key: str = KEY,
    plan_id: str | None = None,
    indexed: bool = True,
    result: str | None = "applied",
) -> str:
    """Write a plan record directly, as an earlier plan (and apply) would have.

    `indexed=False` leaves out object_key, so the record is not in the history index: this
    stands in for an apply the eventually consistent index has not caught up with.
    `result=None` with an applied_at stands in for an apply whose result was never recorded.
    """
    plan_id = plan_id or str(uuid.uuid4())
    item = {
        "plan_id": plan_id,
        "action": "scale_deployment",
        "namespace": "sandbox",
        "name": "web",
        "replicas_from": replicas_from,
        "replicas_to": replicas_to,
        "args_hash": "h",
        "resource_version": "1",
        "generation": 1,
        "uid": key.rsplit("/", 1)[1],
        "created_at": created_at,
        "expires_at": created_at + 300,
        "used": applied_at is not None,
    }
    if indexed:
        item["object_key"] = key
    if applied_at is not None and result is not None:
        item |= {"result": result, "applied_at": applied_at}
    table.put_item(Item=item)
    return plan_id


def test_plan_allows_a_single_revert(clients, table):
    seed(table, 1, 2, NOW - 100, NOW - 90)
    assert plan(clients, replicas=1)["ok"] is True


def test_plan_rejects_second_reversal(clients, table):
    seed(table, 1, 2, NOW - 200, NOW - 190)
    seed(table, 2, 1, NOW - 100, NOW - 90)
    with pytest.raises(GuardError) as exc:
        plan(clients, replicas=3)
    assert exc.value.code == "flapping"
    assert exc.value.details["requested"] == {"from": 2, "to": 3}
    assert [h["to"] for h in exc.value.details["history"]] == [2, 1]
    assert table.scan()["Count"] == 2


def test_recreated_deployment_starts_a_new_history(clients, table):
    old = object_key("sandbox", "web", "8f7c1a2e-0000-4000-8000-000000000002")
    seed(table, 1, 2, NOW - 200, NOW - 190, key=old)
    seed(table, 2, 1, NOW - 100, NOW - 90, key=old)
    for i in range(6):
        seed(table, 1, 2, NOW - 50 - i, NOW - 40 - i, key=old)
    assert plan(clients, replicas=3)["ok"] is True


def test_plan_rejects_object_over_hourly_budget(clients, table):
    for i in range(6):
        seed(table, 1, 2, NOW - 3000 + i, NOW - 2990 + i)
    assert plan_error(clients) == "object_budget_exceeded"


def test_plan_budget_counts_applies_of_plans_created_before_the_window(clients, table):
    # Created 3650 s ago, applied 3590 s ago: inside the hour.
    seed(table, 1, 2, NOW - 3650, NOW - 3590)
    for i in range(5):
        seed(table, 1, 2, NOW - 100 + i, NOW - 90 + i)
    assert plan_error(clients) == "object_budget_exceeded"


def test_plan_rate_limit(clients, table):
    for i in range(19):
        seed(table, 2, 3, NOW - 500 + i)
    assert plan(clients)["ok"] is True  # the 20th plan in the window
    assert plan_error(clients) == "plan_rate_limited"


def test_plan_rate_limit_window_edge(clients, table):
    seed(table, 2, 3, NOW - SETTINGS.plan_rate_window_seconds)
    for i in range(19):
        seed(table, 2, 3, NOW - 10 - i)
    assert plan(clients)["ok"] is True


def test_plan_rejects_manual_change(clients, table):
    last = seed(table, 1, 3, NOW - 100, NOW - 90)
    # Applied 1 -> 3, but the Deployment now has 2.
    clients.apps.read_namespaced_deployment.return_value = deployment(
        annotations={LAST_PLAN_ANNOTATION: last}
    )
    with pytest.raises(GuardError) as exc:
        plan(clients, replicas=3)
    assert exc.value.code == "manual_change"
    assert exc.value.details == {"last_applied_to": 3, "current": 2}
    assert table.scan()["Count"] == 1


def test_plan_with_override_allows_manual_change_and_pins_it(clients, table):
    last = seed(table, 1, 3, NOW - 100, NOW - 90)
    clients.apps.read_namespaced_deployment.return_value = deployment(
        annotations={LAST_PLAN_ANNOTATION: last}
    )
    result = plan(clients, replicas=1, override_manual_change=True)

    assert result["ok"] is True
    data = result["data"]
    assert data["override_manual_change"] is True
    assert {"check": "no_manual_change", "passed": True, "overridden": True} in data["preflight"]
    item = table.get_item(Key={"plan_id": data["plan_id"]})["Item"]
    assert item["args_hash"] == scale_args_hash("sandbox", "web", 1, True)
    assert item["args_hash"] != scale_args_hash("sandbox", "web", 1)


def test_override_without_manual_change_is_not_reported(clients):
    data = plan(clients, override_manual_change=True)["data"]
    assert all("overridden" not in check for check in data["preflight"])


@pytest.mark.parametrize("applied", [False, True])
def test_no_manual_change_check_without_an_applied_plan(clients, table, applied):
    last = seed(table, 1, 3, NOW - 100, NOW - 90 if applied else None)
    annotation = last if not applied else str(uuid.uuid4())  # not applied, or not in the table
    clients.apps.read_namespaced_deployment.return_value = deployment(
        annotations={LAST_PLAN_ANNOTATION: annotation}
    )
    assert plan(clients)["ok"] is True


def test_no_manual_change_when_last_plan_was_for_another_object(clients, table):
    old = object_key("sandbox", "web", "8f7c1a2e-0000-4000-8000-000000000002")
    last = seed(table, 1, 3, NOW - 100, NOW - 90, key=old)
    clients.apps.read_namespaced_deployment.return_value = deployment(
        annotations={LAST_PLAN_ANNOTATION: last}
    )
    assert plan(clients)["ok"] is True


def test_plan_rejects_manual_change_when_the_apply_result_was_not_recorded(clients, table):
    # The patch set the annotation, then record_result failed: the plan is consumed but has
    # no result. The annotation is proof enough that it was applied.
    last = seed(table, 1, 3, NOW - 100, NOW - 90, result=None)
    clients.apps.read_namespaced_deployment.return_value = deployment(
        annotations={LAST_PLAN_ANNOTATION: last}
    )
    assert plan_error(clients, replicas=3) == "manual_change"


def test_plan_sees_flapping_when_the_index_missed_the_last_apply(clients, table):
    seed(table, 1, 2, NOW - 200, NOW - 190)
    last = seed(table, 2, 1, NOW - 100, NOW - 90, indexed=False)
    clients.apps.read_namespaced_deployment.return_value = deployment(
        replicas=1, annotations={LAST_PLAN_ANNOTATION: last}
    )
    with pytest.raises(GuardError) as exc:
        plan(clients, replicas=2)
    assert exc.value.code == "flapping"
    assert [(h["from"], h["to"]) for h in exc.value.details["history"]] == [(1, 2), (2, 1)]


def test_plan_budget_counts_an_apply_the_index_missed(clients, table):
    for i in range(5):
        seed(table, 1, 2, NOW - 100 + i, NOW - 90 + i)
    last = seed(table, 1, 2, NOW - 80, NOW - 70, indexed=False)
    clients.apps.read_namespaced_deployment.return_value = deployment(
        annotations={LAST_PLAN_ANNOTATION: last}
    )
    assert plan_error(clients, replicas=3) == "object_budget_exceeded"


@pytest.mark.parametrize("value", ["true", 1, None])
def test_plan_override_must_be_boolean(clients, value):
    assert plan_error(clients, override_manual_change=value) == "invalid_argument"


def test_plan_fails_closed_when_history_cannot_be_read(wired, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("throttled")

    monkeypatch.setattr(wired.store, "history", broken)
    event = {"namespace": "sandbox", "name": "web", "replicas": 3}
    result = handler.lambda_handler(event, context("plan_scale_deployment"))
    assert result["error"] == {"code": "internal", "message": "internal error"}


def test_plan_fails_closed_when_last_plan_cannot_be_read(wired, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("throttled")

    monkeypatch.setattr(wired.store, "consumed_plan", broken)
    wired.apps.read_namespaced_deployment.return_value = deployment(
        annotations={LAST_PLAN_ANNOTATION: str(uuid.uuid4())}
    )
    event = {"namespace": "sandbox", "name": "web", "replicas": 3}
    result = handler.lambda_handler(event, context("plan_scale_deployment"))
    assert result["error"]["code"] == "internal"


def test_plan_rejects_when_the_global_budget_is_spent(clients, table):
    for _ in range(SETTINGS.global_hourly_apply_budget):
        clients.store.take_global_budget(NOW, SETTINGS.global_hourly_apply_budget)
    assert plan_error(clients) == "global_budget_exceeded"


def test_plan_only_reads_the_global_budget(clients, table):
    assert plan(clients)["ok"] is True
    assert clients.store.global_applies(NOW) == 0


def test_plan_rejects_a_change_within_the_cooldown(clients, table):
    seed(table, 3, 2, NOW - 40, NOW - 30)
    assert plan_error(clients) == "cooldown"


# --- Blast radius and impact preview ------------------------------------------


def test_plan_rejects_a_step_over_the_blast_radius_budget(clients, table):
    clients.apps.read_namespaced_deployment.return_value = deployment(replicas=1)
    with pytest.raises(GuardError) as exc:
        plan(clients, replicas=3)
    assert exc.value.code == "blast_radius_exceeded"
    assert exc.value.details == {"from": 1, "to": 3, "max_step": 1, "max_step_percent": 100}
    assert table.scan()["Count"] == 0
    clients.policy.list_namespaced_pod_disruption_budget.assert_not_called()


def test_plan_allows_a_step_at_the_budget(clients):
    # From 2 at 100 %: up to 4 or down to 0.
    assert plan(clients, replicas=4)["ok"] is True
    assert plan(clients, replicas=0)["ok"] is True


def test_plan_uses_the_configured_step_percent(clients):
    settings = dataclasses.replace(SETTINGS, max_replica_step_percent=50)
    args = {"namespace": "sandbox", "name": "web", "replicas": 4}
    with pytest.raises(GuardError) as exc:
        handler.plan_scale_deployment(args, settings, clients, NOW)
    assert exc.value.code == "blast_radius_exceeded"
    assert exc.value.details["max_step"] == 1


def test_plan_reports_blast_radius_in_preflight(clients):
    checks = [check["check"] for check in plan(clients)["data"]["preflight"]]
    assert checks.index("blast_radius") == checks.index("no_hpa") + 1


def labelled_deployment() -> k8s.V1Deployment:
    dep = deployment()
    dep.spec.template = k8s.V1PodTemplateSpec(
        metadata=k8s.V1ObjectMeta(labels={"app": "web", "tier": "front"})
    )
    return dep


def test_plan_returns_the_measured_impact(clients):
    clients.apps.read_namespaced_deployment.return_value = labelled_deployment()
    clients.policy.list_namespaced_pod_disruption_budget.return_value = raw_list(
        [
            {
                "metadata": {"name": "web"},
                "spec": {"minAvailable": 2, "selector": {"matchLabels": {"app": "web"}}},
                "status": {
                    "currentHealthy": 2,
                    "desiredHealthy": 2,
                    "disruptionsAllowed": 0,
                    "expectedPods": 2,
                },
            }
        ]
    )
    clients.core.list_namespaced_service.return_value = raw_list(
        [
            {"metadata": {"name": "web"}, "spec": {"selector": {"app": "web"}}},
            {"metadata": {"name": "db"}, "spec": {}},
        ]
    )
    clients.discovery.list_namespaced_endpoint_slice.return_value = raw_list(
        [
            {
                "metadata": {"labels": {"kubernetes.io/service-name": "web"}},
                "endpoints": [
                    {"addresses": [f"10.0.0.{i}"], "conditions": {"ready": True}} for i in range(2)
                ],
            },
            # A Service with no pods has a placeholder slice with null endpoints.
            {"metadata": {"labels": {"kubernetes.io/service-name": "db"}}, "endpoints": None},
        ]
    )

    impact = plan(clients, replicas=1)["data"]["impact"]

    assert impact["pods"] == {"ready_now": 2, "from": 2, "to": 1, "added": 0, "removed": 1}
    assert impact["pod_disruption_budgets"] == [
        {
            "name": "web",
            "min_available": 2,
            "max_unavailable": None,
            "current_healthy": 2,
            "desired_healthy": 2,
            "disruptions_allowed": 0,
        }
    ]
    assert impact["services"] == [
        {"name": "web", "ready_endpoints_now": 2, "projected_ready_endpoints": 1}
    ]
    assert [w["code"] for w in impact["warnings"]] == ["pdb_headroom"]
    for call in (
        clients.policy.list_namespaced_pod_disruption_budget,
        clients.core.list_namespaced_service,
        clients.discovery.list_namespaced_endpoint_slice,
    ):
        call.assert_called_once_with(namespace="sandbox", _preload_content=False)


def test_plan_warnings_do_not_block(clients, table):
    data = plan(clients, replicas=0)["data"]
    assert [w["code"] for w in data["impact"]["warnings"]] == ["scale_to_zero"]
    assert table.get_item(Key={"plan_id": data["plan_id"]})["Item"]["replicas_to"] == 0


@pytest.mark.parametrize(
    ("api", "method"),
    [
        ("policy", "list_namespaced_pod_disruption_budget"),
        ("core", "list_namespaced_service"),
        ("discovery", "list_namespaced_endpoint_slice"),
    ],
)
@pytest.mark.parametrize("status", [403, 404, 500])
def test_plan_fails_closed_when_impact_cannot_be_read(clients, table, api, method, status):
    getattr(getattr(clients, api), method).side_effect = ApiException(status=status, reason="x")
    with pytest.raises(GuardError) as exc:
        plan(clients)
    assert exc.value.code == "kubernetes_error"
    assert str(status) in exc.value.message
    assert table.scan()["Count"] == 0


def test_plan_reads_impact_after_the_guard_checks(clients, table):
    seed(table, 3, 2, NOW - 40, NOW - 30)
    assert plan_error(clients) == "cooldown"
    clients.policy.list_namespaced_pod_disruption_budget.assert_not_called()


def test_plan_with_an_uninterpretable_selector_fails_closed(clients, table):
    pdb = {
        "metadata": {"name": "odd"},
        "spec": {"selector": {"matchExpressions": [{"key": "app", "operator": "Near"}]}},
    }
    clients.policy.list_namespaced_pod_disruption_budget.return_value = raw_list([pdb])
    assert plan_error(clients) == "kubernetes_error"
