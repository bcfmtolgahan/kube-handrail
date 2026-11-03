import base64
import importlib.util
import io
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from handrail_common.config import ConfigError, load_settings
from handrail_common.killswitch import KillSwitch
from handrail_common.plans import SCALE_DEPLOYMENT, Plan, PlanStore, scale_args_hash


def _load(name: str, directory: str):
    # Load by path: every Lambda directory contains a module named `handler`.
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).parents[2] / "mcp" / directory / "handler.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


handler = _load("approver_handler", "approver")
write = _load("k8s_write_handler_for_approver", "k8s_write")

NOW = 1_700_000_000
REGION = "us-east-1"
TABLE = "plans-test"
PARAM = "/handrail/remediation-enabled"
ACCOUNT = "123456789012"
ALICE = f"arn:aws:iam::{ACCOUNT}:user/alice"
OPERATOR_ROLE = f"arn:aws:iam::{ACCOUNT}:role/ops/oncall-approver"
OPERATOR_SESSION = f"arn:aws:sts::{ACCOUNT}:assumed-role/oncall-approver/bob"
WRITE_ROLE = f"arn:aws:iam::{ACCOUNT}:role/kube-handrail-k8s-write"
WRITE_SESSION = (
    f"arn:aws:sts::{ACCOUNT}:assumed-role/kube-handrail-k8s-write/kube-handrail-k8s-write"
)
READ_ROLE = f"arn:aws:iam::{ACCOUNT}:role/kube-handrail-k8s-read"
UID = "8f7c1a2e-0000-4000-8000-000000000001"
WRITE_ENV = {
    "CLUSTER_NAME": "test-cluster",
    "PLANS_TABLE": TABLE,
    "ALLOWED_NAMESPACES": "sandbox",
    "MIN_REPLICAS": "0",
    "MAX_REPLICAS": "5",
    "REMEDIATION_ENABLED_PARAM": PARAM,
    "AWS_REGION": REGION,
    "APPROVAL_MODE": "server",
}


def settings(mode: str = "server", approvers=(ALICE, OPERATOR_ROLE, WRITE_ROLE)):
    return handler.ApproverSettings(
        plans_table=TABLE,
        write_function="kube-handrail-k8s-write",
        approvers=tuple(approvers),
        # The tool Lambdas' roles are refused even though WRITE_ROLE is listed above.
        denied=(READ_ROLE, WRITE_ROLE),
        approval_mode=mode,
        apply_after_approval_ttl_seconds=120,
        region=REGION,
    )


class WriteLambda:
    """Stands in for lambda:Invoke of the write function: runs its real handler with the
    ClientContext the approver sent, decoded the way the Lambda runtime decodes it."""

    def __init__(self, write_handler) -> None:
        self.calls: list[dict] = []
        self.write_handler = write_handler
        self.function_error: str | None = None

    def invoke(self, **kwargs):
        self.calls.append(kwargs)
        if self.function_error:
            return {"FunctionError": self.function_error, "Payload": io.BytesIO(b"{}")}
        custom = json.loads(base64.b64decode(kwargs["ClientContext"]))["custom"]
        context = SimpleNamespace(client_context=SimpleNamespace(custom=custom))
        result = self.write_handler(json.loads(kwargs["Payload"]), context)
        return {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(result).encode())}


@pytest.fixture
def env(monkeypatch, plans_table):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        plans_table(TABLE, REGION)
        ssm = boto3.client("ssm", region_name=REGION)
        ssm.put_parameter(Name=PARAM, Value="true", Type="String")
        apps = MagicMock()
        apps.read_namespaced_deployment.return_value = SimpleNamespace(
            metadata=SimpleNamespace(
                generation=4, resource_version="10", uid=UID, labels=None, annotations=None
            ),
            spec=SimpleNamespace(replicas=1),
        )
        apps.patch_namespaced_deployment.return_value = SimpleNamespace(
            metadata=SimpleNamespace(resource_version="11")
        )
        store = PlanStore.from_name(TABLE, REGION)
        write_rt = write.Runtime(
            settings=load_settings(WRITE_ENV, write=True),
            kill_switch=KillSwitch(ssm, PARAM),
            store=store,
            apps=lambda: apps,
            clock=lambda: NOW,
        )
        monkeypatch.setattr(write, "get_runtime", lambda: write_rt)
        lam = WriteLambda(write.lambda_handler)
        rt = handler.Runtime(settings=settings(), store=store, lambda_client=lam, clock=lambda: NOW)
        yield SimpleNamespace(rt=rt, store=store, apps=apps, lam=lam, write_rt=write_rt)


def make_plan(store: PlanStore, replicas: int = 2, override: bool = False) -> Plan:
    plan = Plan(
        plan_id=str(uuid.uuid4()),
        action=SCALE_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=1,
        replicas_to=replicas,
        args_hash=scale_args_hash("sandbox", "web", replicas, override),
        resource_version="10",
        generation=4,
        uid=UID,
        created_at=NOW,
        expires_at=NOW + 300,
        override_manual_change=override,
        impact={"pods": {"ready_now": 1, "from": 1, "to": replicas, "added": 1, "removed": 0}},
    )
    store.create(plan)
    return plan


def event(caller: str | None, method: str = "POST", body=None, query=None, b64=False) -> dict:
    request_context: dict = {"http": {"method": method}}
    if caller is not None:
        request_context["authorizer"] = {"iam": {"userArn": caller, "accountId": ACCOUNT}}
    raw = body if isinstance(body, str) or body is None else json.dumps(body)
    if b64 and raw is not None:
        raw = base64.b64encode(raw.encode()).decode()
    return {
        "requestContext": request_context,
        "queryStringParameters": query,
        "body": raw,
        "isBase64Encoded": b64,
    }


APPROVER_REQUEST_ID = "3f2a7c10-5b9e-4c3d-8e1f-0a2b4c6d8e9f"


def call(rt, *args, **kwargs) -> tuple[int, dict]:
    response = handler.run(rt, event(*args, **kwargs), APPROVER_REQUEST_ID)
    assert response["headers"] == {"content-type": "application/json"}
    return response["statusCode"], json.loads(response["body"])


def approve(env, plan: Plan, caller: str = ALICE, **extra) -> tuple[int, dict]:
    return call(env.rt, caller, body={"plan_id": plan.plan_id, "decision": "approve", **extra})


# --- Identity ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("entry", "caller", "matches"),
    [
        (ALICE, ALICE, True),
        (ALICE, f"arn:aws:iam::{ACCOUNT}:user/alice2", False),
        (OPERATOR_ROLE, OPERATOR_SESSION, True),
        (OPERATOR_ROLE, f"arn:aws:sts::{ACCOUNT}:assumed-role/oncall-approver/any-session", True),
        (OPERATOR_ROLE, "arn:aws:sts::999999999999:assumed-role/oncall-approver/bob", False),
        (OPERATOR_ROLE, f"arn:aws:sts::{ACCOUNT}:assumed-role/oncall-approver-x/bob", False),
        (OPERATOR_ROLE, f"arn:aws-cn:sts::{ACCOUNT}:assumed-role/oncall-approver/bob", False),
        # A role entry never matches the role ARN itself, nor a user of the same name.
        (OPERATOR_ROLE, OPERATOR_ROLE, False),
        (f"arn:aws:iam::{ACCOUNT}:role/alice", ALICE, False),
        (ALICE, f"arn:aws:sts::{ACCOUNT}:assumed-role/alice/s", False),
        ("not-an-arn", "not-an-arn", False),
    ],
)
def test_principal_matching(entry, caller, matches):
    assert handler.principal_matches(entry, caller) is matches


def test_unlisted_principal_is_refused(env):
    plan = make_plan(env.store)
    status, body = approve(env, plan, caller=f"arn:aws:iam::{ACCOUNT}:user/mallory")
    assert (status, body["error"]["code"]) == (403, "forbidden")
    assert env.store.get(plan.plan_id)["status"] == "planned"


def test_tool_lambda_role_is_refused_even_when_listed(env):
    plan = make_plan(env.store)
    status, body = approve(env, plan, caller=WRITE_SESSION)
    assert (status, body["error"]["code"]) == (403, "forbidden")
    assert "machine" in body["error"]["message"]
    assert env.lam.calls == []


def test_request_without_iam_identity_is_refused(env):
    plan = make_plan(env.store)
    status, body = call(env.rt, None, body={"plan_id": plan.plan_id, "decision": "approve"})
    assert (status, body["error"]["code"]) == (403, "forbidden")


def test_assumed_role_session_can_approve(env):
    plan = make_plan(env.store)
    status, body = approve(env, plan, caller=OPERATOR_SESSION, apply=False)
    assert status == 200
    assert body["data"]["plan"]["approval"]["approved_by"] == OPERATOR_SESSION


# --- Approve and apply ---------------------------------------------------------


def test_approve_triggers_the_apply_with_the_plans_own_arguments(env):
    plan = make_plan(env.store, override=True)
    status, body = approve(env, plan)

    assert status == 200 and body["ok"] is True
    (invoke,) = env.lam.calls
    assert invoke["FunctionName"] == "kube-handrail-k8s-write"
    assert invoke["InvocationType"] == "RequestResponse"
    context = json.loads(base64.b64decode(invoke["ClientContext"]))
    assert context == {
        "custom": {
            "bedrockAgentCoreToolName": "k8sw___apply_scale_deployment",
            "kubeHandrailCorrelationId": APPROVER_REQUEST_ID,
        }
    }
    assert json.loads(invoke["Payload"]) == {
        "plan_id": plan.plan_id,
        "namespace": "sandbox",
        "name": "web",
        "replicas": 2,
        "override_manual_change": True,
    }
    applied = body["data"]["apply"]
    assert applied["ok"] is True and applied["data"]["approved"] is True
    assert body["data"]["approval_mode"] == "server"
    view = body["data"]["plan"]
    assert view["status"] == "applied"
    assert view["approval"]["approved_by"] == ALICE
    assert view["result"]["result"] == "applied"
    record = env.store.get(plan.plan_id)
    assert record["approved_by"] == ALICE and record["used"] is True


def test_the_decision_and_its_apply_share_one_correlation_id(env, capsys):
    plan = make_plan(env.store)
    status, body = approve(env, plan)

    assert status == 200
    assert body["data"]["apply"]["data"]["correlation_id"] == APPROVER_REQUEST_ID
    assert body["data"]["plan"]["correlation_ids"] == {
        "decision": APPROVER_REQUEST_ID,
        "apply": APPROVER_REQUEST_ID,
    }
    annotations = env.apps.patch_namespaced_deployment.call_args.args[2]["metadata"]["annotations"]
    assert annotations["kube-handrail.io/correlation-id"] == APPROVER_REQUEST_ID
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    tool, decision = lines
    assert tool["tool"] == "apply_scale_deployment"
    assert tool["correlation_id"] == APPROVER_REQUEST_ID and tool["outcome"] == "ok"
    assert decision == {
        "event": "decision",
        "plan_id": plan.plan_id,
        "decision": "approve",
        "approver": ALICE,
        "correlation_id": APPROVER_REQUEST_ID,
        "apply_outcome": "ok",
    }


def test_a_rejection_logs_no_reason(env, capsys):
    plan = make_plan(env.store)
    body = {"plan_id": plan.plan_id, "decision": "reject", "reason": "SECRET-REASON"}
    call(env.rt, ALICE, body=body)
    out = capsys.readouterr().out
    assert "SECRET-REASON" not in out
    (decision,) = [json.loads(line) for line in out.splitlines()]
    assert decision["decision"] == "reject" and decision["apply_outcome"] is None
    assert env.store.get(plan.plan_id)["decision_correlation_id"] == APPROVER_REQUEST_ID


def test_apply_payload_omits_a_false_override(env):
    plan = make_plan(env.store)
    approve(env, plan)
    assert "override_manual_change" not in json.loads(env.lam.calls[0]["Payload"])


def test_no_apply_only_records_the_approval(env):
    plan = make_plan(env.store)
    status, body = approve(env, plan, apply=False)
    assert status == 200 and body["data"]["apply"] is None
    assert body["data"]["plan"]["status"] == "approved"
    assert env.lam.calls == []
    # A client can still apply it afterwards, within the apply window.
    context = SimpleNamespace(
        client_context=SimpleNamespace(
            custom={"bedrockAgentCoreToolName": "k8sw___apply_scale_deployment"}
        )
    )
    args = {"plan_id": plan.plan_id, "namespace": "sandbox", "name": "web", "replicas": 2}
    assert write.lambda_handler(args, context)["ok"] is True


def test_client_mode_records_the_approval_without_applying(env):
    env.rt.settings = settings(mode="client")
    plan = make_plan(env.store)
    status, body = approve(env, plan)
    assert status == 200 and body["data"]["apply"] is None and env.lam.calls == []


def test_both_mode_records_the_approval_and_leaves_the_apply_to_the_client(env):
    """In both mode the client's own approval must still happen, so the approver never applies."""
    env.rt.settings = settings(mode="both")
    plan = make_plan(env.store)
    status, body = approve(env, plan)
    assert status == 200 and body["data"]["apply"] is None and env.lam.calls == []
    assert body["data"]["plan"]["status"] == "approved"
    assert body["data"]["approval_mode"] == "both"
    assert not settings(mode="both").applies_after_approval
    assert settings(mode="server").applies_after_approval


def test_an_invoke_error_reports_the_recorded_approval(env):
    class Failing:
        def invoke(self, **_kwargs):
            raise ClientError(
                {"Error": {"Code": "TooManyRequestsException", "Message": "x"}}, "Invoke"
            )

    env.rt.lambda_client = Failing()
    plan = make_plan(env.store)
    status, body = approve(env, plan)
    assert status == 200 and body["ok"] is True
    error = body["data"]["apply"]["error"]
    assert error["code"] == "internal"
    assert (
        error["message"]
        == "approval recorded; apply could not be started: TooManyRequestsException"
    )
    assert body["data"]["plan"]["status"] == "approved"


def test_a_failed_apply_is_reported_and_the_approval_kept(env):
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name=PARAM, Value="false", Type="String", Overwrite=True
    )
    plan = make_plan(env.store)
    status, body = approve(env, plan)
    assert status == 200
    assert body["data"]["apply"]["error"]["code"] == "remediation_disabled"
    assert body["data"]["plan"]["status"] == "approved"


def test_a_crashed_apply_is_internal(env):
    env.lam.function_error = "Unhandled"
    plan = make_plan(env.store)
    status, body = approve(env, plan)
    assert status == 200 and body["data"]["apply"]["error"]["code"] == "internal"


def test_approve_twice_is_refused(env):
    plan = make_plan(env.store)
    approve(env, plan, apply=False)
    status, body = approve(env, plan)
    assert (status, body["error"]["code"]) == (409, "plan_decided")
    assert body["error"]["details"] == {"status": "approved"}
    assert env.lam.calls == []


def test_approve_used_or_expired_plan_is_refused(env):
    used = make_plan(env.store)
    approve(env, used)
    status, body = approve(env, used)
    assert (status, body["error"]["code"]) == (409, "plan_used")

    expired = make_plan(env.store)
    env.rt.clock = lambda: NOW + 300
    status, body = approve(env, expired)
    assert (status, body["error"]["code"]) == (409, "plan_expired")

    status, body = call(env.rt, ALICE, body={"plan_id": str(uuid.uuid4()), "decision": "approve"})
    assert (status, body["error"]["code"]) == (404, "plan_not_found")


# --- Reject ----------------------------------------------------------------------


def test_reject_records_who_and_why(env):
    plan = make_plan(env.store)
    status, body = call(
        env.rt,
        ALICE,
        body={"plan_id": plan.plan_id, "decision": "reject", "reason": "wrong\x07 target"},
    )
    assert status == 200 and body["data"]["apply"] is None
    rejection = body["data"]["plan"]["rejection"]
    assert rejection == {"rejected_by": ALICE, "rejected_at": NOW, "reason": "wrong target"}
    assert body["data"]["plan"]["status"] == "rejected"
    assert env.lam.calls == []
    status, body = approve(env, plan)
    assert (status, body["error"]["details"]) == (409, {"status": "rejected"})


# --- Show ------------------------------------------------------------------------


def test_show_renders_the_server_record(env):
    plan = make_plan(env.store)
    status, body = call(env.rt, ALICE, method="GET", query={"plan_id": plan.plan_id})
    assert status == 200
    view = body["data"]
    assert view["plan_id"] == plan.plan_id and view["status"] == "planned"
    assert view["replicas"] == {"from": 1, "to": 2}
    assert view["impact"]["pods"]["added"] == 1
    assert view["requested_by"] == "mcp-gateway"


def test_show_requires_an_approver_too(env):
    plan = make_plan(env.store)
    status, _ = call(env.rt, WRITE_SESSION, method="GET", query={"plan_id": plan.plan_id})
    assert status == 403


# --- Malformed requests ------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        None,
        "",
        "not json",
        "[1, 2]",
        {"decision": "approve"},
        {"plan_id": "x", "decision": "approve"},
        {"plan_id": "PLAN", "decision": "maybe"},
        {"plan_id": "PLAN", "decision": "approve", "apply": "yes"},
        {"plan_id": "PLAN", "decision": "approve", "replicas": 5},
        {"plan_id": "PLAN", "decision": "reject", "reason": "x" * 501},
        {"plan_id": "PLAN", "decision": "reject", "reason": ["x"]},
        {"plan_id": "PLAN", "decision": "approve", "pad": "x" * 5000},
    ],
)
def test_malformed_requests(env, body):
    plan = make_plan(env.store)
    if isinstance(body, dict) and body.get("plan_id") == "PLAN":
        body = {**body, "plan_id": plan.plan_id}
    status, response = call(env.rt, ALICE, body=body)
    assert (status, response["error"]["code"]) == (400, "invalid_argument")
    assert env.store.get(plan.plan_id)["status"] == "planned"


def test_base64_body_is_decoded(env):
    plan = make_plan(env.store)
    status, _ = call(
        env.rt,
        ALICE,
        body={"plan_id": plan.plan_id, "decision": "approve", "apply": False},
        b64=True,
    )
    assert status == 200


@pytest.mark.parametrize("method", ["PUT", "DELETE", None])
def test_other_methods_are_refused(env, method):
    status, body = call(env.rt, ALICE, method=method)
    assert (status, body["error"]["code"]) == (400, "invalid_argument")


def test_show_requires_a_plan_id(env):
    status, body = call(env.rt, ALICE, method="GET", query=None)
    assert (status, body["error"]["code"]) == (400, "invalid_argument")


def test_unexpected_errors_are_internal(env, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("table gone")

    monkeypatch.setattr(env.store, "get", broken)
    status, body = call(env.rt, ALICE, method="GET", query={"plan_id": str(uuid.uuid4())})
    assert (status, body["error"]["code"]) == (500, "internal")


def test_non_mapping_event_is_forbidden(env):
    response = handler.run(env.rt, "junk")
    assert response["statusCode"] == 403


# --- Configuration -----------------------------------------------------------------

BASE_ENV = {
    "PLANS_TABLE": TABLE,
    "WRITE_FUNCTION_NAME": "kube-handrail-k8s-write",
    "APPROVER_PRINCIPALS": f"{ALICE}, {OPERATOR_ROLE}",
    "DENIED_PRINCIPALS": f"{READ_ROLE},{WRITE_ROLE}",
}


def test_settings_parse():
    s = handler.load_approver_settings(BASE_ENV)
    assert s.approvers == (ALICE, OPERATOR_ROLE)
    assert s.denied == (READ_ROLE, WRITE_ROLE)
    assert s.approval_mode == "server" and s.applies_after_approval
    assert s.apply_after_approval_ttl_seconds == 120
    assert handler.load_approver_settings(BASE_ENV | {"APPROVER_PRINCIPALS": ""}).approvers == ()


@pytest.mark.parametrize(
    "override",
    [
        {"PLANS_TABLE": ""},
        {"WRITE_FUNCTION_NAME": ""},
        {"APPROVER_PRINCIPALS": OPERATOR_SESSION},
        {"APPROVER_PRINCIPALS": "*"},
        {"DENIED_PRINCIPALS": "arn:aws:iam::123:role/x"},
        {"APPROVAL_MODE": "none"},
        {"APPLY_AFTER_APPROVAL_TTL_SECONDS": "0"},
    ],
)
def test_settings_invalid(override):
    with pytest.raises(ConfigError):
        handler.load_approver_settings(BASE_ENV | override)
