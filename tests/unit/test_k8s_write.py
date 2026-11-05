import dataclasses
import importlib.util
import json
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
from handrail_common.plans import (
    RESTART_ANNOTATION,
    RESTART_DEPLOYMENT,
    ROLLBACK_DEPLOYMENT,
    SCALE_DEPLOYMENT,
    Plan,
    PlanStore,
    plan_args_hash,
    restart_args,
    rollback_args,
    scale_args,
    scale_args_hash,
    template_hash,
)
from handrail_common.validation import GuardError

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
        args=scale_args("sandbox", "web", replicas, override),
        override_manual_change=override,
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


def test_apply_rejects_a_manual_change_made_before_a_restart(rt):
    runtime_, apps = rt
    seed(1, 2, NOW - 600)
    last = seed(1, 1, NOW - 300)  # set back to 1 by hand, then restarted at 1
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: last}
    )
    plan = create_plan(runtime_.store)

    response = apply(plan.plan_id)

    assert error_code(response) == "manual_change"
    assert response["error"]["details"] == {"last_applied_to": 2, "current": 1}
    apps.patch_namespaced_deployment.assert_not_called()


def test_apply_after_a_restart_compares_against_the_restart(rt):
    runtime_, apps = rt
    last = seed(2, 2, NOW - 300)  # no earlier scale; the Deployment now has 1
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: last}
    )
    assert error_code(apply(create_plan(runtime_.store).plan_id)) == "manual_change"


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


def test_a_record_cannot_hide_the_override_from_the_approver(rt):
    """The approval view shows the record field; an override only in args is refused."""
    runtime_, apps = rt
    plan = create_plan(runtime_.store, override=True)
    runtime_.store._table.update_item(
        Key={"plan_id": plan.plan_id},
        UpdateExpression="SET override_manual_change = :f",
        ExpressionAttributeValues={":f": False},
    )
    assert error_code(apply(plan.plan_id, override_manual_change=True)) == "plan_mismatch"
    apps.read_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "plan_mismatch"


def test_a_record_without_args_cannot_be_applied(rt):
    """Plans made before the argument payload was recorded must be planned again."""
    runtime_, apps = rt
    plan = create_plan(runtime_.store)
    runtime_.store._table.update_item(Key={"plan_id": plan.plan_id}, UpdateExpression="REMOVE args")
    assert error_code(apply(plan.plan_id)) == "plan_expired"
    apps.read_namespaced_deployment.assert_not_called()


def test_a_record_with_altered_args_cannot_be_applied(rt):
    runtime_, apps = rt
    plan = create_plan(runtime_.store)
    runtime_.store._table.update_item(
        Key={"plan_id": plan.plan_id},
        UpdateExpression="SET args.override_manual_change = :t",
        ExpressionAttributeValues={":t": True},
    )
    assert error_code(apply(plan.plan_id)) == "plan_mismatch"
    apps.read_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "plan_mismatch"


# --- apply_restart_deployment ---------------------------------------------------

RESTART_CONTEXT = SimpleNamespace(
    client_context=SimpleNamespace(
        custom={
            "bedrockAgentCoreToolName": "k8sw___apply_restart_deployment",
            "bedrockAgentCoreAwsRequestId": GATEWAY_REQUEST_ID,
        }
    ),
    aws_request_id="lambda-request-id",
)


def create_restart_plan(store: PlanStore, **overrides) -> Plan:
    plan = Plan(
        plan_id=str(uuid.uuid4()),
        action=RESTART_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=1,
        replicas_to=1,
        args_hash=plan_args_hash(RESTART_DEPLOYMENT, restart_args("sandbox", "web")),
        args=restart_args("sandbox", "web"),
        resource_version="12345",
        generation=4,
        uid=UID,
        created_at=NOW,
        expires_at=NOW + 300,
        template_from="sha256:from",
        template_to="sha256:to",
    )
    plan = dataclasses.replace(plan, **overrides)
    store.create(plan)
    return plan


def apply_restart(plan_id: str, **overrides) -> dict:
    event = {"plan_id": plan_id, "namespace": "sandbox", "name": "web"}
    return handler.lambda_handler({**event, **overrides}, RESTART_CONTEXT)


def test_restart_sets_only_the_plan_annotations(rt):
    runtime_, apps = rt
    plan = create_restart_plan(runtime_.store)

    response = apply_restart(plan.plan_id)

    assert response["ok"] is True
    assert response["data"] == {
        "plan_id": plan.plan_id,
        "namespace": "sandbox",
        "name": "web",
        "pods_replaced": 1,
        "template": "sha256:to",
        "resource_version": "12346",
        "approved": False,
        "correlation_id": GATEWAY_REQUEST_ID,
    }
    dry, real = apps.patch_namespaced_deployment.call_args_list
    assert dry.kwargs == {"dry_run": "All"} and real.kwargs == {}
    # A dict body: the client sends it as a merge patch. No spec.replicas.
    assert real.args == (
        "web",
        "sandbox",
        {
            "metadata": {
                "resourceVersion": "12345",
                "annotations": {
                    handler.LAST_PLAN_ANNOTATION: plan.plan_id,
                    "kube-handrail.io/correlation-id": GATEWAY_REQUEST_ID,
                },
            },
            "spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: plan.plan_id}}}},
        },
    )
    assert item(plan.plan_id)["result"] == "applied"


def test_restart_kill_switch_off(aws, monkeypatch):
    runtime_, apps = runtime(enabled="false")
    monkeypatch.setattr(handler, "get_runtime", lambda: runtime_)
    plan = create_restart_plan(runtime_.store)
    assert error_code(apply_restart(plan.plan_id)) == "remediation_disabled"
    apps.read_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["used"] is False


def test_restart_of_a_paused_rollout_is_conflict(rt):
    runtime_, apps = rt
    paused = deployment()
    paused.spec.paused = True
    apps.read_namespaced_deployment.return_value = paused
    plan = create_restart_plan(runtime_.store)
    response = apply_restart(plan.plan_id)
    assert error_code(response) == "conflict"
    assert response["error"]["details"] == {"paused": True}
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "conflict"


def test_restart_after_a_spec_change_is_conflict(rt):
    runtime_, apps = rt
    apps.read_namespaced_deployment.return_value = deployment(generation=5)
    plan = create_restart_plan(runtime_.store)
    assert error_code(apply_restart(plan.plan_id)) == "conflict"
    apps.patch_namespaced_deployment.assert_not_called()


def test_restart_within_the_cooldown_is_refused(rt):
    runtime_, apps = rt
    seed(1, 1, NOW - 30)
    assert error_code(apply_restart(create_restart_plan(runtime_.store).plan_id)) == "cooldown"
    apps.patch_namespaced_deployment.assert_not_called()


def test_restart_skips_the_manual_change_check(rt):
    runtime_, apps = rt
    # The last apply left 3 replicas and the Deployment has 1.
    last = seed(1, 3, NOW - 500)
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: last}
    )
    assert apply_restart(create_restart_plan(runtime_.store).plan_id)["ok"] is True


def test_restart_takes_one_unit_of_the_global_budget(rt):
    runtime_, _ = rt
    apply_restart(create_restart_plan(runtime_.store).plan_id)
    assert runtime_.store.global_applies(NOW) == 1


def test_restart_ignores_the_blast_radius_setting(rt):
    runtime_, _ = rt
    runtime_.settings = dataclasses.replace(runtime_.settings, max_replica_step_percent=1)
    assert apply_restart(create_restart_plan(runtime_.store).plan_id)["ok"] is True


def test_restart_plan_cannot_be_applied_as_a_scale(rt):
    runtime_, apps = rt
    plan = create_restart_plan(runtime_.store)
    assert error_code(apply(plan.plan_id, replicas=1)) == "plan_mismatch"
    apps.patch_namespaced_deployment.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"replicas_to": 2},
        {"template_to": None},
        {"override_manual_change": True},
        {"name": "other"},
    ],
)
def test_an_altered_restart_record_cannot_be_applied(rt, change):
    runtime_, apps = rt
    plan = create_restart_plan(runtime_.store, **change)
    assert error_code(apply_restart(plan.plan_id)) == "plan_mismatch"
    apps.patch_namespaced_deployment.assert_not_called()


def test_server_mode_restart_after_approval(server_rt):
    runtime_, apps = server_rt
    plan = create_restart_plan(runtime_.store)
    assert error_code(apply_restart(plan.plan_id)) == "approval_required"
    plan = create_restart_plan(runtime_.store)
    runtime_.store.approve(plan.plan_id, HUMAN, NOW, 120)
    response = apply_restart(plan.plan_id)
    assert response["ok"] is True and response["data"]["approved"] is True


# --- apply_rollback_deployment --------------------------------------------------

ROLLBACK_CONTEXT = SimpleNamespace(
    client_context=SimpleNamespace(
        custom={
            "bedrockAgentCoreToolName": "k8sw___apply_rollback_deployment",
            "bedrockAgentCoreAwsRequestId": GATEWAY_REQUEST_ID,
        }
    ),
    aws_request_id="lambda-request-id",
)
RS_UID = "8f7c1a2e-0000-4000-8000-0000000000aa"
# The template of the target ReplicaSet as the API server returns it, with a field the
# client models do not know: the patch must carry it unchanged.
TARGET_TEMPLATE = {
    "metadata": {"labels": {"app": "web"}},
    "spec": {
        "containers": [
            {
                "name": "nginx",
                "image": "nginx:1.27.4",
                "env": [{"name": "MODE", "value": "old"}],
                "futureField": {"x": 1},
            }
        ]
    },
}
TARGET_HASH = template_hash(TARGET_TEMPLATE)


def target_rs(template: dict | None = None, uid: str = RS_UID, owner: str = UID) -> SimpleNamespace:
    """The ReplicaSet as read with _preload_content=False."""
    tmpl = json.loads(json.dumps(template or TARGET_TEMPLATE))
    tmpl["metadata"]["labels"]["pod-template-hash"] = "7d5b9c6f8"
    rs = {
        "metadata": {
            "name": "web-7d5b9c6f8",
            "uid": uid,
            "annotations": {"deployment.kubernetes.io/revision": "3"},
            "ownerReferences": [{"kind": "Deployment", "uid": owner, "controller": True}],
        },
        "spec": {"replicas": 0, "template": tmpl},
    }
    return SimpleNamespace(data=json.dumps(rs).encode())


def create_rollback_plan(store: PlanStore, override: bool = False, **overrides) -> Plan:
    plan = Plan(
        plan_id=str(uuid.uuid4()),
        action=ROLLBACK_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=1,
        replicas_to=1,
        args_hash=plan_args_hash(ROLLBACK_DEPLOYMENT, rollback_args("sandbox", "web", 3, override)),
        args=rollback_args("sandbox", "web", 3, override),
        override_manual_change=override,
        resource_version="12345",
        generation=4,
        uid=UID,
        created_at=NOW,
        expires_at=NOW + 300,
        template_from="sha256:current",
        template_to=TARGET_HASH,
        revision_from=5,
        revision_to=3,
        target_rs_name="web-7d5b9c6f8",
        target_rs_uid=RS_UID,
        images=[{"container": "nginx", "init": False, "from": "a", "to": "b", "changed": True}],
        fields_changed=["spec.containers[nginx].image"],
    )
    plan = dataclasses.replace(plan, **overrides)
    store.create(plan)
    return plan


def apply_rollback(plan_id: str, **overrides) -> dict:
    event = {"plan_id": plan_id, "namespace": "sandbox", "name": "web", "revision": 3}
    return handler.lambda_handler({**event, **overrides}, ROLLBACK_CONTEXT)


@pytest.fixture
def rollback_rt(rt):
    runtime_, apps = rt
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={"deployment.kubernetes.io/revision": "5"}
    )
    apps.read_namespaced_replica_set.return_value = target_rs()
    return runtime_, apps


def test_rollback_writes_exactly_the_checked_template_as_a_json_patch(rollback_rt):
    runtime_, apps = rollback_rt
    plan = create_rollback_plan(runtime_.store)

    response = apply_rollback(plan.plan_id)

    assert response["ok"] is True
    assert response["data"] == {
        "plan_id": plan.plan_id,
        "namespace": "sandbox",
        "name": "web",
        "revision_requested": 3,
        "template": TARGET_HASH,
        "images": plan.images,
        "resource_version": "12346",
        "approved": False,
        "correlation_id": GATEWAY_REQUEST_ID,
    }
    apps.read_namespaced_replica_set.assert_called_once_with(
        "web-7d5b9c6f8", "sandbox", _preload_content=False
    )
    dry, real = apps.patch_namespaced_deployment.call_args_list
    assert dry.kwargs == {"dry_run": "All"} and real.kwargs == {}
    assert dry.args == real.args
    # A list body: the client sends it as a JSON Patch. The resourceVersion op is first.
    assert real.args == (
        "web",
        "sandbox",
        [
            {"op": "replace", "path": "/metadata/resourceVersion", "value": "12345"},
            {"op": "replace", "path": "/spec/template", "value": TARGET_TEMPLATE},
            {
                "op": "add",
                "path": "/metadata/annotations/kube-handrail.io~1last-plan-id",
                "value": plan.plan_id,
            },
            {
                "op": "add",
                "path": "/metadata/annotations/kube-handrail.io~1correlation-id",
                "value": GATEWAY_REQUEST_ID,
            },
        ],
    )
    record = item(plan.plan_id)
    assert record["used"] is True and record["result"] == "applied"


def test_rollback_patch_adds_the_annotation_map_when_there_is_none():
    ops = handler.rollback_patch({"spec": {}}, "9", "p-1", "c-1", has_annotations=False)
    assert ops[-1] == {
        "op": "add",
        "path": "/metadata/annotations",
        "value": {handler.LAST_PLAN_ANNOTATION: "p-1", "kube-handrail.io/correlation-id": "c-1"},
    }
    assert len(ops) == 3


def test_rollback_patch_escapes_json_pointer_tokens():
    assert handler._pointer("a/b~c") == "a~1b~0c"
    ops = handler.rollback_patch({"spec": {}}, "9", "p-1")
    assert [op["path"] for op in ops] == [
        "/metadata/resourceVersion",
        "/spec/template",
        "/metadata/annotations/kube-handrail.io~1last-plan-id",
    ]


def test_rollback_on_a_deployment_without_annotations(rollback_rt):
    runtime_, apps = rollback_rt
    apps.read_namespaced_deployment.return_value = deployment()
    apply_rollback(create_rollback_plan(runtime_.store).plan_id)
    body = apps.patch_namespaced_deployment.call_args.args[2]
    assert body[-1]["path"] == "/metadata/annotations"


def test_rollback_kill_switch_off_makes_no_kubernetes_call(aws, monkeypatch):
    runtime_, apps = runtime(enabled="false")
    monkeypatch.setattr(handler, "get_runtime", lambda: runtime_)
    plan = create_rollback_plan(runtime_.store)
    assert error_code(apply_rollback(plan.plan_id)) == "remediation_disabled"
    apps.read_namespaced_deployment.assert_not_called()
    apps.read_namespaced_replica_set.assert_not_called()
    assert item(plan.plan_id)["used"] is False


def test_rollback_of_a_removed_replica_set_is_revision_not_found(rollback_rt):
    runtime_, apps = rollback_rt
    apps.read_namespaced_replica_set.side_effect = ApiException(status=404, reason="Not Found")
    plan = create_rollback_plan(runtime_.store)
    response = apply_rollback(plan.plan_id)
    assert error_code(response) == "revision_not_found"
    assert response["error"]["details"] == {"revision": 3, "replica_set": "web-7d5b9c6f8"}
    apps.patch_namespaced_deployment.assert_not_called()
    record = item(plan.plan_id)
    assert record["used"] is True and record["result"] == "revision_not_found"


def test_rollback_replica_set_read_error_is_kubernetes_error(rollback_rt):
    runtime_, apps = rollback_rt
    apps.read_namespaced_replica_set.side_effect = ApiException(status=403, reason="Forbidden")
    assert error_code(apply_rollback(create_rollback_plan(runtime_.store).plan_id)) == (
        "kubernetes_error"
    )


@pytest.mark.parametrize(
    "rs", [target_rs(uid="8f7c1a2e-0000-4000-8000-0000000000bb"), target_rs(owner="other")]
)
def test_rollback_to_a_replaced_replica_set_is_conflict(rollback_rt, rs):
    runtime_, apps = rollback_rt
    apps.read_namespaced_replica_set.return_value = rs
    response = apply_rollback(create_rollback_plan(runtime_.store).plan_id)
    assert error_code(response) == "conflict"
    current = json.loads(rs.data)["metadata"]["uid"]
    assert response["error"]["details"] == {"replica_set": {"planned": RS_UID, "current": current}}
    apps.patch_namespaced_deployment.assert_not_called()


def test_rollback_to_an_edited_replica_set_is_conflict(rollback_rt):
    runtime_, apps = rollback_rt
    edited = json.loads(json.dumps(TARGET_TEMPLATE))
    edited["spec"]["containers"][0]["image"] = "evil:1"
    apps.read_namespaced_replica_set.return_value = target_rs(edited)
    response = apply_rollback(create_rollback_plan(runtime_.store).plan_id)
    assert error_code(response) == "conflict"
    assert response["error"]["details"] == {
        "template": {"planned": TARGET_HASH, "current": template_hash(edited)}
    }
    apps.patch_namespaced_deployment.assert_not_called()


def test_rollback_after_a_spec_change_is_conflict(rollback_rt):
    runtime_, apps = rollback_rt
    apps.read_namespaced_deployment.return_value = deployment(generation=5)
    assert error_code(apply_rollback(create_rollback_plan(runtime_.store).plan_id)) == "conflict"
    apps.read_namespaced_replica_set.assert_not_called()
    apps.patch_namespaced_deployment.assert_not_called()


def test_rollback_of_a_paused_rollout_is_conflict(rollback_rt):
    runtime_, apps = rollback_rt
    paused = deployment()
    paused.spec.paused = True
    apps.read_namespaced_deployment.return_value = paused
    response = apply_rollback(create_rollback_plan(runtime_.store).plan_id)
    assert error_code(response) == "conflict"
    assert response["error"]["details"] == {"paused": True}
    apps.patch_namespaced_deployment.assert_not_called()


def test_rollback_conflict_retry_rereads_both_objects(rollback_rt):
    runtime_, apps = rollback_rt
    apps.read_namespaced_deployment.side_effect = [
        deployment(resource_version="1"),
        deployment(resource_version="2"),
    ]
    apps.patch_namespaced_deployment.side_effect = [
        conflict(),
        None,
        SimpleNamespace(metadata=SimpleNamespace(resource_version="3")),
    ]
    response = apply_rollback(create_rollback_plan(runtime_.store).plan_id)
    assert response["ok"] is True
    assert apps.read_namespaced_deployment.call_count == 2
    assert apps.read_namespaced_replica_set.call_count == 2
    versions = [c.args[2][0]["value"] for c in apps.patch_namespaced_deployment.call_args_list]
    assert versions == ["1", "2", "2"]


def test_rollback_retry_sees_a_replica_set_edited_in_between(rollback_rt):
    runtime_, apps = rollback_rt
    edited = json.loads(json.dumps(TARGET_TEMPLATE))
    edited["metadata"]["annotations"] = {"touched": "1"}
    apps.read_namespaced_replica_set.side_effect = [target_rs(), target_rs(edited)]
    apps.patch_namespaced_deployment.side_effect = conflict()
    response = apply_rollback(create_rollback_plan(runtime_.store).plan_id)
    assert error_code(response) == "conflict" and "template" in response["error"]["details"]
    assert apps.patch_namespaced_deployment.call_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"revision_to": 4},
        {"target_rs_uid": None},
        {"target_rs_name": None},
        {"template_to": None},
        {"template_from": None},
        {"replicas_to": 2},
        {"name": "other"},
    ],
)
def test_an_altered_rollback_record_cannot_be_applied(rollback_rt, change):
    runtime_, apps = rollback_rt
    plan = create_rollback_plan(runtime_.store, **change)
    assert error_code(apply_rollback(plan.plan_id)) == "plan_mismatch"
    apps.read_namespaced_deployment.assert_not_called()


def test_rollback_with_another_revision_is_plan_mismatch(rollback_rt):
    runtime_, apps = rollback_rt
    plan = create_rollback_plan(runtime_.store)
    assert error_code(apply_rollback(plan.plan_id, revision=4)) == "plan_mismatch"
    assert item(plan.plan_id)["used"] is False
    assert apply_rollback(plan.plan_id)["ok"] is True


def test_rollback_record_cannot_hide_the_override(rollback_rt):
    runtime_, apps = rollback_rt
    plan = create_rollback_plan(runtime_.store, override=True, override_manual_change=False)
    assert error_code(apply_rollback(plan.plan_id, override_manual_change=True)) == "plan_mismatch"


def rollback_seed(template_from: str, template_to: str) -> str:
    plan_id = seed(1, 1, NOW - 500)
    boto3.resource("dynamodb", region_name=REGION).Table(TABLE).update_item(
        Key={"plan_id": plan_id},
        UpdateExpression="SET template_from = :f, template_to = :t",
        ExpressionAttributeValues={":f": template_from, ":t": template_to},
    )
    return plan_id


def test_rollback_apply_refuses_to_undo_a_template_change_made_by_hand(rollback_rt):
    runtime_, apps = rollback_rt
    last = rollback_seed("sha256:before", "sha256:applied")
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: last}
    )
    plan = create_rollback_plan(runtime_.store)
    response = apply_rollback(plan.plan_id)
    assert error_code(response) == "manual_change"
    assert response["error"]["details"] == {
        "last_applied_template": "sha256:applied",
        "current_template": "sha256:current",
    }
    apps.patch_namespaced_deployment.assert_not_called()
    assert item(plan.plan_id)["result"] == "manual_change"


def test_rollback_apply_with_the_planned_override(rollback_rt):
    runtime_, apps = rollback_rt
    last = rollback_seed("sha256:before", "sha256:applied")
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: last}
    )
    plan = create_rollback_plan(runtime_.store, override=True)
    assert apply_rollback(plan.plan_id, override_manual_change=True)["ok"] is True


def test_rollback_apply_after_its_own_template_change(rollback_rt):
    runtime_, apps = rollback_rt
    last = rollback_seed("sha256:before", "sha256:current")
    apps.read_namespaced_deployment.return_value = deployment(
        annotations={handler.LAST_PLAN_ANNOTATION: last}
    )
    # The Deployment's template is still the one the last apply left: no manual change.
    assert apply_rollback(create_rollback_plan(runtime_.store).plan_id)["ok"] is True


def test_rollback_within_the_cooldown_is_refused(rollback_rt):
    runtime_, apps = rollback_rt
    seed(1, 1, NOW - 30)
    assert error_code(apply_rollback(create_rollback_plan(runtime_.store).plan_id)) == "cooldown"
    apps.patch_namespaced_deployment.assert_not_called()


def test_rollback_takes_one_unit_of_the_global_budget(rollback_rt):
    runtime_, _ = rollback_rt
    apply_rollback(create_rollback_plan(runtime_.store).plan_id)
    assert runtime_.store.global_applies(NOW) == 1


def test_server_mode_rollback_after_approval(server_rt):
    runtime_, apps = server_rt
    apps.read_namespaced_replica_set.return_value = target_rs()
    plan = create_rollback_plan(runtime_.store)
    assert error_code(apply_rollback(plan.plan_id)) == "approval_required"
    plan = create_rollback_plan(runtime_.store)
    runtime_.store.approve(plan.plan_id, HUMAN, NOW, 120)
    response = apply_rollback(plan.plan_id)
    assert response["ok"] is True and response["data"]["approved"] is True


# --- Agent identity -----------------------------------------------------------

AGENTS = '{"client-a": {"name": "smoke_test"}, "client-b": {"name": "devops_agent"}}'
IDENTITY_ENV = ENV | {"CALLER_INTERCEPTOR": "true", "AGENTS": AGENTS}
APPROVAL_CONTEXT = SimpleNamespace(
    client_context=SimpleNamespace(
        custom={
            "bedrockAgentCoreToolName": "k8sw___apply_scale_deployment",
            "kubeHandrailChannel": "approval-api",
        }
    ),
    aws_request_id="approver-request-id",
)


@pytest.fixture
def identity_rt(aws, monkeypatch):
    # dispatch trusts the caller argument only with the interceptor, as in the Lambda.
    monkeypatch.setenv("CALLER_INTERCEPTOR", "true")
    rt, apps = runtime()
    rt.settings = load_settings(IDENTITY_ENV, write=True)
    monkeypatch.setattr(handler, "get_runtime", lambda: rt)
    return rt, apps


def create_agent_plan(store: PlanStore, agent: str | None = "smoke_test") -> Plan:
    plan = create_plan(store)
    plan = dataclasses.replace(
        plan,
        plan_id=str(uuid.uuid4()),
        requested_by=f"agent:{agent}" if agent else "mcp-gateway",
        agent=agent,
        requested_client_id="client-a" if agent else None,
    )
    store.create(plan)
    return plan


def apply_as(plan_id: str, client: str | None, context=CONTEXT) -> dict:
    event = {"plan_id": plan_id, "namespace": "sandbox", "name": "web", "replicas": 3}
    if client is not None:
        event["_kube_handrail_caller"] = client
    return handler.lambda_handler(event, context)


def test_identity_apply_by_the_requesting_agent(identity_rt):
    runtime_, apps = identity_rt
    plan = create_agent_plan(runtime_.store)

    assert apply_as(plan.plan_id, "client-a")["ok"] is True
    record = item(plan.plan_id)
    assert (record["used"], record["channel"], record["result"]) == (True, "gateway", "applied")
    annotations = apps.patch_namespaced_deployment.call_args.args[2]["metadata"]["annotations"]
    assert annotations[handler.REQUESTED_BY_ANNOTATION] == "agent:smoke_test"


def test_identity_apply_by_another_agent_is_forbidden(identity_rt):
    runtime_, apps = identity_rt
    plan = create_agent_plan(runtime_.store)

    response = apply_as(plan.plan_id, "client-b")
    assert error_code(response) == "forbidden"
    assert response["error"]["details"] == {"reason": "not_requester"}
    assert item(plan.plan_id)["used"] is False
    apps.read_namespaced_deployment.assert_not_called()


@pytest.mark.parametrize("client", [None, "unknown", "client-z"])
def test_identity_apply_through_the_gateway_needs_a_known_caller(identity_rt, client):
    runtime_, apps = identity_rt
    plan = create_agent_plan(runtime_.store)

    response = apply_as(plan.plan_id, client)
    assert (error_code(response), response["error"]["details"]) == (
        "forbidden",
        {"reason": "agent_unknown"},
    )
    assert item(plan.plan_id)["used"] is False


def test_identity_apply_of_a_legacy_plan_is_forbidden(identity_rt):
    """A plan made before identity mode names the gateway class; it must be planned again."""
    runtime_, _ = identity_rt
    plan = create_plan(runtime_.store)
    response = apply_as(plan.plan_id, "client-a")
    assert response["error"]["details"] == {"reason": "not_requester"}
    assert item(plan.plan_id)["used"] is False


def test_identity_apply_on_the_approval_channel_needs_no_caller(identity_rt):
    runtime_, _ = identity_rt
    plan = create_agent_plan(runtime_.store)

    # A caller argument on this channel is not consulted.
    assert apply_as(plan.plan_id, "client-b", APPROVAL_CONTEXT)["ok"] is True
    assert item(plan.plan_id)["channel"] == "approval-api"


def test_identity_approval_channel_refuses_a_plan_without_an_agent(identity_rt):
    runtime_, apps = identity_rt
    plan = create_plan(runtime_.store)
    response = apply_as(plan.plan_id, None, APPROVAL_CONTEXT)
    assert response["error"]["details"] == {"reason": "agent_unknown"}
    record = item(plan.plan_id)
    assert (record["used"], record["result"]) == (True, "forbidden")
    apps.patch_namespaced_deployment.assert_not_called()


def test_legacy_apply_records_the_channel_and_no_agent_annotation(rt):
    runtime_, apps = rt
    plan = create_plan(runtime_.store)
    assert apply(plan.plan_id, _kube_handrail_caller="client-a")["ok"] is True
    assert item(plan.plan_id)["channel"] == "gateway"
    annotations = apps.patch_namespaced_deployment.call_args.args[2]["metadata"]["annotations"]
    assert handler.REQUESTED_BY_ANNOTATION not in annotations


def test_rollback_patch_sets_the_requested_by_annotation():
    ops = handler.rollback_patch({"spec": {}}, "7", "p", "c", requested_by="agent:smoke_test")
    assert ops[-1] == {
        "op": "add",
        "path": "/metadata/annotations/kube-handrail.io~1requested-by",
        "value": "agent:smoke_test",
    }


@pytest.mark.parametrize("tool", sorted(handler.HANDLERS))
def test_every_apply_tool_runs_the_agent_guard(rt, monkeypatch, tool):
    runtime_, apps = rt

    def refuse(settings):
        raise GuardError("forbidden", "guard ran", {"reason": "agent_unknown"})

    monkeypatch.setattr(handler, "guard_apply", refuse)
    with pytest.raises(GuardError, match="guard ran"):
        handler.HANDLERS[tool]({"plan_id": str(uuid.uuid4())})
    apps.read_namespaced_deployment.assert_not_called()
