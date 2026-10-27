import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest
from kubernetes import client as k8s
from kubernetes.client.rest import ApiException
from moto import mock_aws

from handrail_common.config import Settings
from handrail_common.plans import PlanStore, scale_args_hash
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
def table(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        yield dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "plan_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "plan_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )


@pytest.fixture
def clients(table):
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = deployment()
    autoscaling = MagicMock()
    autoscaling.list_namespaced_horizontal_pod_autoscaler.return_value = (
        k8s.V2HorizontalPodAutoscalerList(items=[])
    )
    return SimpleNamespace(apps=apps, autoscaling=autoscaling, store=PlanStore(table))


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


def test_plan_defaults_missing_replicas_to_one(clients):
    clients.apps.read_namespaced_deployment.return_value = deployment(replicas=None)
    assert plan(clients)["data"]["diff"]["replicas"]["from"] == 1


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
