import dataclasses
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
    # Most plans here go from 1 to 3; the blast-radius budget has its own tests.
    "MAX_REPLICA_STEP_PERCENT": "200",
    # The guard cases below are the same in every mode; server approval has its own tests.
    "APPROVAL_MODE": "client",
}
GATEWAY_REQUEST_ID = "6c1f9a52-1f0e-4d7e-9a0b-3d1c2b4a5e6f"
CONTEXT = SimpleNamespace(
    client_context=SimpleNamespace(
        custom={
            "bedrockAgentCoreToolName": "k8sw___apply_scale_deployment",
            "bedrockAgentCoreAwsRequestId": GATEWAY_REQUEST_ID,
        }
    ),
    aws_request_id="lambda-request-id",
)


@pytest.fixture
def aws(monkeypatch, plans_table):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        plans_table(TABLE, REGION)
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


def create_plan(
    store: PlanStore, replicas: int = 3, expires_at: int = NOW + 300, override: bool = False
) -> Plan:
    plan = Plan(
        plan_id=str(uuid.uuid4()),
        action=SCALE_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=1,
        replicas_to=replicas,
        args_hash=scale_args_hash("sandbox", "web", replicas, override),
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
        "approved": False,
        "correlation_id": GATEWAY_REQUEST_ID,
    }
    apps.read_namespaced_deployment.assert_called_once_with("web", "sandbox")
    dry, real = apps.patch_namespaced_deployment.call_args_list
    assert dry.kwargs == {"dry_run": "All"}
    assert real.kwargs == {}
    body = real.args[2]
    assert dry.args == real.args == ("web", "sandbox", body)
    assert body["spec"] == {"replicas": 3}
    assert body["metadata"]["resourceVersion"] == "12345"
    # Both annotations change in the one patch, so the apply bumps generation once.
    assert body["metadata"]["annotations"] == {
        handler.LAST_PLAN_ANNOTATION: plan.plan_id,
        "kube-handrail.io/correlation-id": GATEWAY_REQUEST_ID,
    }

    record = item(plan.plan_id)
    assert record["used"] is True
    assert record["result"] == "applied"
    assert record["applied_at"] == NOW
    assert record["apply_correlation_id"] == GATEWAY_REQUEST_ID


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


def test_a_refused_apply_records_its_correlation_id(rt):
    runtime_, apps = rt
    plan = create_plan(runtime_.store)
    apps.read_namespaced_deployment.return_value = deployment(generation=5)

    assert apply(plan.plan_id)["error"]["code"] == "conflict"
    record = item(plan.plan_id)
    assert record["result"] == "conflict"
    assert record["apply_correlation_id"] == GATEWAY_REQUEST_ID


def test_scale_patch_without_a_correlation_id_sets_only_the_plan():
    body = handler.scale_patch(2, "7", "p")
    assert body["metadata"]["annotations"] == {handler.LAST_PLAN_ANNOTATION: "p"}


def test_record_failure_does_not_fail_apply(rt, monkeypatch):
    runtime_, _ = rt
    plan = create_plan(runtime_.store)

    def broken(*_args, **_kwargs):
        raise RuntimeError("table unavailable")

    monkeypatch.setattr(runtime_.store, "record_result", broken)
    assert apply(plan.plan_id)["ok"] is True


# --- Oscillation breaker, re-checked after consume and the spec check -------

KEY = f"sandbox/web/{UID}"


def seed(
    replicas_from: int,
    replicas_to: int,
    applied_at: int,
    key: str = KEY,
    indexed: bool = True,
    result: str | None = "applied",
) -> str:
    """An earlier applied plan for the object.

    `indexed=False` leaves out object_key, so the record is not in the history index (an
    apply the eventually consistent index has not caught up with). `result=None` is an apply
    whose result was never recorded.
    """
    plan_id = str(uuid.uuid4())
    item = {
        "plan_id": plan_id,
        "action": SCALE_DEPLOYMENT,
        "namespace": "sandbox",
        "name": "web",
        "replicas_from": replicas_from,
        "replicas_to": replicas_to,
        "args_hash": "h",
        "resource_version": "1",
        "generation": 1,
        "uid": key.rsplit("/", 1)[1],
        "created_at": applied_at - 10,
        "expires_at": applied_at + 290,
        "used": True,
    }
    if indexed:
        item["object_key"] = key
    if result is not None:
        item |= {"result": result, "applied_at": applied_at}
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).put_item(Item=item)
    return plan_id


def test_apply_rejects_flapping_after_consuming_the_plan(rt):
    runtime_, apps = rt
    seed(1, 2, NOW - 200)
    seed(2, 1, NOW - 100)
    plan = create_plan(runtime_.store)  # 1 -> 3 reverses the last change a second time

    response = apply(plan.plan_id)

    assert error_code(response) == "flapping"
    assert response["error"]["details"]["history"] == [
        {"applied_at": NOW - 200, "from": 1, "to": 2},
        {"applied_at": NOW - 100, "from": 2, "to": 1},
    ]
    apps.patch_namespaced_deployment.assert_not_called()
    record = item(plan.plan_id)
    assert record["used"] is True
    assert record["result"] == "flapping"


def test_apply_allows_a_single_revert(rt):
    runtime_, _ = rt
    seed(3, 1, NOW - 100)
    assert apply(create_plan(runtime_.store).plan_id)["ok"] is True


def test_apply_history_of_a_replaced_object_does_not_count(rt):
    runtime_, _ = rt
    old = "sandbox/web/8f7c1a2e-0000-4000-8000-000000000002"
    seed(1, 2, NOW - 200, key=old)
    seed(2, 1, NOW - 100, key=old)
    assert apply(create_plan(runtime_.store).plan_id)["ok"] is True


def test_apply_rejects_object_over_hourly_budget(rt):
    runtime_, apps = rt
    for i in range(6):
        seed(1, 3, NOW - 3000 + i)
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert error_code(response) == "object_budget_exceeded"
    assert response["error"]["details"]["count"] == 6
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "object_budget_exceeded"


def test_apply_budget_does_not_count_the_plan_being_applied(rt):
    runtime_, _ = rt
    for i in range(5):
        seed(1, 3, NOW - 3000 + i)
    assert apply(create_plan(runtime_.store).plan_id)["ok"] is True


def test_apply_does_not_check_the_plan_rate(rt):
    runtime_, _ = rt
    plans = [create_plan(runtime_.store) for _ in range(25)]
    assert apply(plans[-1].plan_id)["ok"] is True


def test_apply_rejects_manual_change(rt):
    runtime_, apps = rt
    last = seed(3, 2, NOW - 100)  # kube-handrail left it at 2; it now has 1
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: last}
    )
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert error_code(response) == "manual_change"
    assert response["error"]["details"] == {"last_applied_to": 2, "current": 1}
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "manual_change"


def test_apply_rejects_manual_change_when_the_apply_result_was_not_recorded(rt):
    runtime_, apps = rt
    last = seed(3, 2, NOW - 100, result=None)  # patched to 2, then record_result failed
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: last}
    )
    plan = create_plan(runtime_.store)

    assert error_code(apply(plan.plan_id)) == "manual_change"
    apps.patch_namespaced_deployment.assert_not_called()


def test_apply_rejects_flapping_when_the_index_missed_the_last_apply(rt):
    runtime_, apps = rt
    seed(1, 2, NOW - 200)
    last = seed(2, 1, NOW - 100, indexed=False)  # not in the index yet; the annotation names it
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: last}
    )
    plan = create_plan(runtime_.store)  # 1 -> 3: a second reversal

    response = apply(plan.plan_id)

    assert error_code(response) == "flapping"
    assert [(h["from"], h["to"]) for h in response["error"]["details"]["history"]] == [
        (1, 2),
        (2, 1),
    ]
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "flapping"


def test_apply_budget_counts_an_apply_the_index_missed(rt):
    runtime_, apps = rt
    for i in range(5):
        seed(1, 3, NOW - 3000 + i)
    # Unrecorded, so it counts as applied at expires_at; old enough to be past the cooldown.
    last = seed(3, 1, NOW - 400, indexed=False, result=None)
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: last}
    )
    plan = create_plan(runtime_.store)

    assert error_code(apply(plan.plan_id)) == "object_budget_exceeded"
    apps.patch_namespaced_deployment.assert_not_called()


def test_apply_with_override_allows_manual_change(rt):
    runtime_, apps = rt
    last = seed(3, 2, NOW - 100)
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: last}
    )
    plan = create_plan(runtime_.store, override=True)

    assert apply(plan.plan_id, override_manual_change=True)["ok"] is True
    assert item(plan.plan_id)["result"] == "applied"


@pytest.mark.parametrize(("planned", "applied"), [(True, None), (True, False), (False, True)])
def test_override_flag_must_match_the_plan(rt, planned, applied):
    runtime_, apps = rt
    plan = create_plan(runtime_.store, override=planned)
    overrides = {} if applied is None else {"override_manual_change": applied}

    assert error_code(apply(plan.plan_id, **overrides)) == "plan_mismatch"
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["used"] is False


def test_override_false_matches_a_plan_without_it(rt):
    runtime_, _ = rt
    plan = create_plan(runtime_.store)
    assert apply(plan.plan_id, override_manual_change=False)["ok"] is True


def test_apply_override_must_be_boolean(rt):
    runtime_, _ = rt
    plan = create_plan(runtime_.store)
    assert error_code(apply(plan.plan_id, override_manual_change="yes")) == "invalid_argument"
    assert item(plan.plan_id)["used"] is False


@pytest.mark.parametrize("method", ["history", "consumed_plan"])
def test_apply_fails_closed_when_history_cannot_be_read(rt, monkeypatch, method):
    runtime_, apps = rt
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: str(uuid.uuid4())}
    )
    plan = create_plan(runtime_.store)

    def broken(*_args, **_kwargs):
        raise RuntimeError("throttled")

    monkeypatch.setattr(runtime_.store, method, broken)
    response = apply(plan.plan_id)

    assert response["error"] == {"code": "internal", "message": "internal error"}
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "internal"


def test_apply_checks_history_after_the_spec_check(rt, monkeypatch):
    runtime_, apps = rt
    apps.read_namespaced_deployment.return_value = deployment(generation=5)
    plan = create_plan(runtime_.store)
    calls = []
    monkeypatch.setattr(runtime_.store, "history", lambda *a: calls.append(a) or [])

    assert error_code(apply(plan.plan_id)) == "conflict"
    assert calls == []


def test_apply_checks_history_once_across_retries(rt, monkeypatch):
    runtime_, apps = rt
    apps.patch_namespaced_deployment.side_effect = [
        conflict(),
        SimpleNamespace(metadata=SimpleNamespace(resource_version="12347")),
        SimpleNamespace(metadata=SimpleNamespace(resource_version="12348")),
    ]
    plan = create_plan(runtime_.store)
    calls = []
    monkeypatch.setattr(runtime_.store, "history", lambda *a: calls.append(a) or [])

    assert apply(plan.plan_id)["ok"] is True
    assert len(calls) == 1


def test_apply_takes_one_unit_of_the_global_budget(rt):
    runtime_, _ = rt
    assert apply(create_plan(runtime_.store).plan_id)["ok"] is True
    assert runtime_.store.global_applies(NOW) == 1


def test_apply_rejects_when_the_global_budget_is_spent(rt):
    runtime_, apps = rt
    for _ in range(runtime_.settings.global_hourly_apply_budget):
        runtime_.store.take_global_budget(NOW, runtime_.settings.global_hourly_apply_budget)
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert error_code(response) == "global_budget_exceeded"
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "global_budget_exceeded"


def test_apply_refused_by_another_check_does_not_use_the_global_budget(rt):
    runtime_, _ = rt
    for i in range(6):
        seed(1, 3, NOW - 3000 + i)
    assert error_code(apply(create_plan(runtime_.store).plan_id)) == "object_budget_exceeded"
    assert runtime_.store.global_applies(NOW) == 0


def test_apply_rechecks_the_blast_radius_with_the_current_setting(rt):
    runtime_, apps = rt
    plan = create_plan(runtime_.store)  # 1 -> 3, within 200 % when it was planned
    runtime_.settings = dataclasses.replace(runtime_.settings, max_replica_step_percent=100)

    response = apply(plan.plan_id)

    assert error_code(response) == "blast_radius_exceeded"
    assert response["error"]["details"] == {
        "from": 1,
        "to": 3,
        "max_step": 1,
        "max_step_percent": 100,
    }
    apps.read_namespaced_deployment.assert_not_called()
    apps.patch_namespaced_deployment.assert_not_called()
    record = item(plan.plan_id)
    assert record["used"] is True
    assert record["result"] == "blast_radius_exceeded"
    assert runtime_.store.global_applies(NOW) == 0


def test_apply_allows_a_step_at_the_budget(rt):
    runtime_, _ = rt
    runtime_.settings = dataclasses.replace(runtime_.settings, max_replica_step_percent=100)
    assert apply(create_plan(runtime_.store, replicas=2).plan_id, replicas=2)["ok"] is True


def test_apply_rejects_a_change_within_the_cooldown(rt):
    runtime_, apps = rt
    seed(3, 1, NOW - 30)
    response = apply(create_plan(runtime_.store).plan_id)
    assert error_code(response) == "cooldown"
    assert response["error"]["details"]["retry_after"] == NOW + 30
    apps.patch_namespaced_deployment.assert_not_called()


# --- Server-side approval ------------------------------------------------------

HUMAN = "arn:aws:iam::123456789012:user/alice"


@pytest.fixture(params=["server", "both"])
def server_rt(aws, monkeypatch, request):
    rt, apps = runtime()
    rt.settings = load_settings(ENV | {"APPROVAL_MODE": request.param}, write=True)
    monkeypatch.setattr(handler, "get_runtime", lambda: rt)
    return rt, apps


def test_server_mode_apply_without_approval_is_refused(server_rt):
    runtime_, apps = server_rt
    plan = create_plan(runtime_.store)
    response = apply(plan.plan_id)
    assert error_code(response) == "approval_required"
    assert response["error"]["details"] == {"status": "planned"}
    apps.read_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["used"] is False


def test_server_mode_apply_after_approval(server_rt):
    runtime_, apps = server_rt
    plan = create_plan(runtime_.store)
    runtime_.store.approve(plan.plan_id, HUMAN, NOW, 120)
    response = apply(plan.plan_id)
    assert response["ok"] is True
    # The client learns that an approval covered it; the record names the approver.
    assert response["data"]["approved"] is True
    assert HUMAN not in str(response)
    record = item(plan.plan_id)
    assert record["used"] is True and record["result"] == "applied"
    assert record["approved_by"] == HUMAN
    assert len(apps.patch_namespaced_deployment.call_args_list) == 2


def test_server_mode_approval_does_not_cover_other_arguments(server_rt):
    runtime_, apps = server_rt
    plan = create_plan(runtime_.store)
    runtime_.store.approve(plan.plan_id, HUMAN, NOW, 120)
    assert error_code(apply(plan.plan_id, replicas=2)) == "plan_mismatch"
    assert error_code(apply(plan.plan_id, override_manual_change=True)) == "plan_mismatch"
    apps.patch_namespaced_deployment.assert_not_called()


def test_server_mode_expired_approval(server_rt):
    runtime_, _ = server_rt
    plan = create_plan(runtime_.store)
    runtime_.store.approve(plan.plan_id, HUMAN, NOW - 120, 120)
    response = apply(plan.plan_id)
    assert error_code(response) == "approval_required"
    assert response["error"]["details"] == {"status": "approval_expired"}


def test_server_mode_rejected_plan(server_rt):
    runtime_, _ = server_rt
    plan = create_plan(runtime_.store)
    runtime_.store.reject(plan.plan_id, HUMAN, NOW - 1, "no")
    response = apply(plan.plan_id)
    assert response["error"]["details"] == {"status": "rejected"}


def test_server_mode_guard_still_applies_after_approval(server_rt):
    runtime_, apps = server_rt
    plan = create_plan(runtime_.store)
    runtime_.store.approve(plan.plan_id, HUMAN, NOW, 120)
    apps.read_namespaced_deployment.return_value = deployment(generation=5)
    assert error_code(apply(plan.plan_id)) == "conflict"
    apps.patch_namespaced_deployment.assert_not_called()


def test_client_mode_needs_no_approval(rt):
    runtime_, _ = rt
    assert runtime_.settings.approval_mode == "client"
    plan = create_plan(runtime_.store)
    response = apply(plan.plan_id)
    assert response["ok"] is True and response["data"]["approved"] is False


@pytest.mark.parametrize("change", [{"namespace": "other"}, {"name": "api"}, {"replicas_to": 2}])
def test_a_record_altered_in_the_table_cannot_redirect_the_apply(rt, change):
    """Matching args_hash is not enough: the record itself must describe the request."""
    runtime_, apps = rt
    plan = create_plan(runtime_.store)
    names = {f"#{k}": k for k in change}
    runtime_.store._table.update_item(
        Key={"plan_id": plan.plan_id},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in change),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={f":{k}": v for k, v in change.items()},
    )
    assert error_code(apply(plan.plan_id)) == "plan_mismatch"
    apps.read_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "plan_mismatch"
