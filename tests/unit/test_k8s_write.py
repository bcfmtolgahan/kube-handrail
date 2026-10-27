import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest
from kubernetes.client.exceptions import ApiException
from moto import mock_aws

from handrail_common.config import load_settings
from handrail_common.killswitch import KillSwitch
from handrail_common.plans import SCALE_DEPLOYMENT, Plan, PlanStore, scale_args_hash

# Load by path: both Lambda directories contain a module named `handler`.
_SPEC = importlib.util.spec_from_file_location(
    "k8s_write_handler", Path(__file__).parents[2] / "mcp" / "k8s_write" / "handler.py"
)
handler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(handler)

NOW = 1_700_000_000
REGION = "us-east-1"
TABLE = "plans-test"
PARAM = "/handrail/remediation-enabled"
ENV = {
    "CLUSTER_NAME": "test-cluster",
    "PLANS_TABLE": TABLE,
    "ALLOWED_NAMESPACES": "sandbox",
    "MIN_REPLICAS": "0",
    "MAX_REPLICAS": "5",
    "REMEDIATION_ENABLED_PARAM": PARAM,
    "AWS_REGION": REGION,
}
CONTEXT = SimpleNamespace(
    client_context=SimpleNamespace(
        custom={"bedrockAgentCoreToolName": "k8sw___apply_scale_deployment"}
    )
)


@pytest.fixture
def aws(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        boto3.resource("dynamodb", region_name=REGION).create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "plan_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "plan_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


UID = "8f7c1a2e-0000-4000-8000-000000000001"


def deployment(
    generation: int = 4,
    replicas: int | None = 1,
    resource_version: str = "12345",
    uid: str = UID,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(
            generation=generation,
            resource_version=resource_version,
            uid=uid,
            labels=labels,
            annotations=annotations,
        ),
        spec=SimpleNamespace(replicas=replicas),
    )


def conflict() -> ApiException:
    return ApiException(status=409, reason="Conflict")


def runtime(enabled: str = "true") -> tuple[handler.Runtime, MagicMock]:
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(Name=PARAM, Value=enabled, Type="String", Overwrite=True)
    apps = MagicMock()
    apps.read_namespaced_deployment.return_value = deployment()
    apps.patch_namespaced_deployment.return_value = SimpleNamespace(
        metadata=SimpleNamespace(resource_version="12346")
    )
    rt = handler.Runtime(
        settings=load_settings(ENV, write=True),
        kill_switch=KillSwitch(ssm, PARAM),
        store=PlanStore.from_name(TABLE, REGION),
        apps=lambda: apps,
        clock=lambda: NOW,
    )
    return rt, apps


@pytest.fixture
def rt(aws, monkeypatch):
    rt, apps = runtime()
    monkeypatch.setattr(handler, "get_runtime", lambda: rt)
    return rt, apps


def create_plan(store: PlanStore, replicas: int = 3, expires_at: int = NOW + 300) -> Plan:
    plan = Plan(
        plan_id=str(uuid.uuid4()),
        action=SCALE_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=1,
        replicas_to=replicas,
        args_hash=scale_args_hash("sandbox", "web", replicas),
        resource_version="12345",
        generation=4,
        uid=UID,
        created_at=NOW,
        expires_at=expires_at,
    )
    store.create(plan)
    return plan


def apply(plan_id: str, replicas: int = 3, **overrides) -> dict:
    event = {"plan_id": plan_id, "namespace": "sandbox", "name": "web", "replicas": replicas}
    return handler.lambda_handler({**event, **overrides}, CONTEXT)


def item(plan_id: str) -> dict:
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    return table.get_item(Key={"plan_id": plan_id}, ConsistentRead=True)["Item"]


def error_code(response: dict) -> str:
    assert response["ok"] is False
    return response["error"]["code"]


def patched_versions(apps: MagicMock) -> list[str]:
    calls = apps.patch_namespaced_deployment.call_args_list
    return [c.args[2]["metadata"]["resourceVersion"] for c in calls]


def test_apply_success_dry_run_then_real(rt):
    runtime_, apps = rt
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert response["ok"] is True
    assert response["data"] == {
        "plan_id": plan.plan_id,
        "namespace": "sandbox",
        "name": "web",
        "replicas_from": 1,
        "replicas_to": 3,
        "resource_version": "12346",
    }
    apps.read_namespaced_deployment.assert_called_once_with("web", "sandbox")
    dry, real = apps.patch_namespaced_deployment.call_args_list
    assert dry.kwargs == {"dry_run": "All"}
    assert real.kwargs == {}
    body = real.args[2]
    assert dry.args == real.args == ("web", "sandbox", body)
    assert body["spec"] == {"replicas": 3}
    assert body["metadata"]["resourceVersion"] == "12345"
    assert body["metadata"]["annotations"] == {handler.LAST_PLAN_ANNOTATION: plan.plan_id}

    record = item(plan.plan_id)
    assert record["used"] is True
    assert record["result"] == "applied"
    assert record["applied_at"] == NOW


def test_kill_switch_off(aws, monkeypatch):
    runtime_, apps = runtime(enabled="false")
    monkeypatch.setattr(handler, "get_runtime", lambda: runtime_)
    plan = create_plan(runtime_.store)

    assert error_code(apply(plan.plan_id)) == "remediation_disabled"
    apps.read_namespaced_deployment.assert_not_called()
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["used"] is False


def test_kill_switch_checked_before_arguments(aws, monkeypatch):
    runtime_, _ = runtime(enabled="false")
    monkeypatch.setattr(handler, "get_runtime", lambda: runtime_)
    assert error_code(apply("not-a-uuid", namespace="kube-system")) == "remediation_disabled"


def test_reused_plan(rt):
    runtime_, apps = rt
    plan = create_plan(runtime_.store)

    assert apply(plan.plan_id)["ok"] is True
    assert error_code(apply(plan.plan_id)) == "plan_used"
    assert apps.patch_namespaced_deployment.call_count == 2


def test_expired_plan(rt):
    runtime_, apps = rt
    plan = create_plan(runtime_.store, expires_at=NOW - 1)

    assert error_code(apply(plan.plan_id)) == "plan_expired"
    apps.patch_namespaced_deployment.assert_not_called()


def test_mismatched_args(rt):
    runtime_, apps = rt
    plan = create_plan(runtime_.store, replicas=3)

    assert error_code(apply(plan.plan_id, replicas=4)) == "plan_mismatch"
    assert error_code(apply(plan.plan_id, name="other")) == "plan_mismatch"
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["used"] is False


def test_unknown_plan(rt):
    assert error_code(apply(str(uuid.uuid4()))) == "plan_not_found"


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"plan_id": "not-a-uuid"}, "invalid_argument"),
        ({"namespace": "kube-system"}, "forbidden_namespace"),
        ({"replicas": 6}, "out_of_bounds"),
        ({"replicas": True}, "invalid_argument"),
    ],
)
def test_arguments_rechecked(rt, overrides, code):
    runtime_, apps = rt
    plan = create_plan(runtime_.store)

    plan_id = overrides.pop("plan_id", plan.plan_id)
    assert error_code(apply(plan_id, **overrides)) == code
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["used"] is False


def test_status_only_change_applies_with_current_resource_version(rt):
    runtime_, apps = rt
    # A rollout updated status: resourceVersion moved on, generation and replicas did not.
    apps.read_namespaced_deployment.return_value = deployment(resource_version="12399")
    plan = create_plan(runtime_.store)

    assert apply(plan.plan_id)["ok"] is True
    assert patched_versions(apps) == ["12399", "12399"]
    assert item(plan.plan_id)["result"] == "applied"


def test_missing_replicas_counts_as_one(rt):
    runtime_, apps = rt
    apps.read_namespaced_deployment.return_value = deployment(replicas=None)
    plan = create_plan(runtime_.store)

    assert apply(plan.plan_id)["ok"] is True


def test_generation_changed_is_conflict(rt):
    runtime_, apps = rt
    apps.read_namespaced_deployment.return_value = deployment(generation=5)
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert error_code(response) == "conflict"
    assert response["error"]["details"] == {
        "generation": {"planned": 4, "current": 5},
        "replicas": {"planned_from": 1, "current": 1},
    }
    apps.patch_namespaced_deployment.assert_not_called()
    record = item(plan.plan_id)
    assert record["used"] is True
    assert record["result"] == "conflict"


def test_replicas_changed_is_conflict(rt):
    runtime_, apps = rt
    # Someone scaled the Deployment directly after the plan was made.
    apps.read_namespaced_deployment.return_value = deployment(generation=5, replicas=2)
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert error_code(response) == "conflict"
    assert response["error"]["details"]["replicas"] == {"planned_from": 1, "current": 2}
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "conflict"


def test_plan_without_generation_is_conflict(rt):
    runtime_, apps = rt
    plan = create_plan(runtime_.store)
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    table.update_item(Key={"plan_id": plan.plan_id}, UpdateExpression="REMOVE generation")

    response = apply(plan.plan_id)

    assert error_code(response) == "conflict"
    assert response["error"]["details"]["generation"] == {"planned": -1, "current": 4}
    apps.patch_namespaced_deployment.assert_not_called()


def test_plan_without_uid_is_conflict(rt):
    runtime_, apps = rt
    plan = create_plan(runtime_.store)
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    table.update_item(
        Key={"plan_id": plan.plan_id},
        UpdateExpression="REMOVE #uid",
        ExpressionAttributeNames={"#uid": "uid"},
    )

    response = apply(plan.plan_id)

    assert error_code(response) == "conflict"
    assert response["error"]["details"] == {"uid": {"planned": "", "current": UID}}
    apps.patch_namespaced_deployment.assert_not_called()


def test_recreated_deployment_is_conflict(rt):
    runtime_, apps = rt
    # Deleted and recreated with the same name: generation and replicas match the plan again.
    other = "8f7c1a2e-0000-4000-8000-000000000002"
    apps.read_namespaced_deployment.return_value = deployment(generation=4, replicas=1, uid=other)
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert error_code(response) == "conflict"
    assert response["error"]["details"] == {"uid": {"planned": UID, "current": other}}
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "conflict"


@pytest.mark.parametrize(
    "marker",
    [
        {"labels": {"app.kubernetes.io/instance": "web"}},
        {"annotations": {"argocd.argoproj.io/tracking-id": "x:apps/Deployment:sandbox/web"}},
    ],
)
def test_gitops_marker_added_after_plan_is_rejected(rt, marker):
    runtime_, apps = rt
    # A label change leaves generation untouched, so only a fresh GitOps check catches it.
    apps.read_namespaced_deployment.return_value = deployment(**marker)
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert error_code(response) == "gitops_managed"
    apps.patch_namespaced_deployment.assert_not_called()
    record = item(plan.plan_id)
    assert record["used"] is True
    assert record["result"] == "gitops_managed"


def test_gitops_marker_detected_on_retry(rt):
    runtime_, apps = rt
    apps.read_namespaced_deployment.side_effect = [
        deployment(),
        deployment(labels={"app.kubernetes.io/instance": "web"}),
    ]
    apps.patch_namespaced_deployment.side_effect = conflict()
    plan = create_plan(runtime_.store)

    assert error_code(apply(plan.plan_id)) == "gitops_managed"
    assert apps.patch_namespaced_deployment.call_count == 1


def test_conflict_on_dry_run_is_retried(rt):
    runtime_, apps = rt
    apps.read_namespaced_deployment.side_effect = [
        deployment(resource_version="12346"),
        deployment(resource_version="12347"),
    ]
    apps.patch_namespaced_deployment.side_effect = [
        conflict(),
        SimpleNamespace(metadata=SimpleNamespace(resource_version="12347")),
        SimpleNamespace(metadata=SimpleNamespace(resource_version="12348")),
    ]
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert response["ok"] is True
    assert response["data"]["resource_version"] == "12348"
    assert apps.read_namespaced_deployment.call_count == 2
    assert patched_versions(apps) == ["12346", "12347", "12347"]
    assert item(plan.plan_id)["result"] == "applied"


def test_conflict_on_real_patch_is_retried(rt):
    runtime_, apps = rt
    apps.read_namespaced_deployment.side_effect = [
        deployment(resource_version="12346"),
        deployment(resource_version="12347"),
    ]
    apps.patch_namespaced_deployment.side_effect = [
        SimpleNamespace(metadata=SimpleNamespace(resource_version="12346")),
        conflict(),
        SimpleNamespace(metadata=SimpleNamespace(resource_version="12347")),
        SimpleNamespace(metadata=SimpleNamespace(resource_version="12348")),
    ]
    plan = create_plan(runtime_.store)

    assert apply(plan.plan_id)["ok"] is True
    assert patched_versions(apps) == ["12346", "12346", "12347", "12347"]


def test_conflict_on_every_attempt(rt):
    runtime_, apps = rt
    apps.patch_namespaced_deployment.side_effect = conflict()
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert error_code(response) == "conflict"
    assert "details" not in response["error"]
    assert apps.read_namespaced_deployment.call_count == handler.PATCH_ATTEMPTS
    assert apps.patch_namespaced_deployment.call_count == handler.PATCH_ATTEMPTS
    record = item(plan.plan_id)
    assert record["used"] is True
    assert record["result"] == "conflict"


def test_spec_change_detected_on_retry(rt):
    runtime_, apps = rt
    apps.read_namespaced_deployment.side_effect = [deployment(), deployment(generation=5)]
    apps.patch_namespaced_deployment.side_effect = conflict()
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert error_code(response) == "conflict"
    assert response["error"]["details"]["generation"] == {"planned": 4, "current": 5}
    assert apps.read_namespaced_deployment.call_count == 2
    assert apps.patch_namespaced_deployment.call_count == 1
    assert item(plan.plan_id)["result"] == "conflict"


@pytest.mark.parametrize("status, code", [(404, "not_found"), (403, "kubernetes_error")])
def test_read_api_errors(rt, status, code):
    runtime_, apps = rt
    apps.read_namespaced_deployment.side_effect = ApiException(status=status)
    plan = create_plan(runtime_.store)

    assert error_code(apply(plan.plan_id)) == code
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == code


@pytest.mark.parametrize("status, code", [(404, "not_found"), (403, "kubernetes_error")])
def test_patch_api_errors(rt, status, code):
    runtime_, apps = rt
    apps.patch_namespaced_deployment.side_effect = ApiException(status=status)
    plan = create_plan(runtime_.store)

    assert error_code(apply(plan.plan_id)) == code
    assert apps.patch_namespaced_deployment.call_count == 1
    assert item(plan.plan_id)["result"] == code


def test_record_failure_does_not_fail_apply(rt, monkeypatch):
    runtime_, _ = rt
    plan = create_plan(runtime_.store)

    def broken(*_args, **_kwargs):
        raise RuntimeError("table unavailable")

    monkeypatch.setattr(runtime_.store, "record_result", broken)
    assert apply(plan.plan_id)["ok"] is True
