"""Server-side approval through the approver Lambda, with the caller ARN its Function URL
reports for a SigV4-signed request (tests/e2e/e2e_gateway.py: approval_tests,
identity_approval_tests). The approval's apply runs the write Lambda in process."""

from harness import (
    AGENT_B,
    AGENT_SWITCHES,
    APPROVER,
    BUCKET,
    CLIENT_B,
    LAMBDA_SESSION,
    NOT_APPROVER,
    code,
    deployment_manifest,
    details,
    load_script,
    stream_records,
    unique,
)

REQUEST_APPROVAL = "k8sr___request_approval"
LAST_PLAN_ANNOTATION = "kube-handrail.io/last-plan-id"
CORRELATION_ANNOTATION = "kube-handrail.io/correlation-id"


def audit_findings(kh, plan_id):
    """audit.py verify of the plan, after the audit Lambda wrote the stream so far."""
    assert kh.run_audit(stream_records(kh.stream_arn())) == {"batchItemFailures": []}
    return load_script("audit").verify_plan(kh.s3, BUCKET, plan_id)["findings"]


def decide(kh, plan_id, decision, caller=APPROVER, **extra):
    return kh.approver("POST", caller, body={"plan_id": plan_id, "decision": decision, **extra})


def test_server_approval_applies_the_plan(kh):
    kh.configure(APPROVAL_MODE="server")
    target = kh.create(deployment_manifest(unique("it-approval")))
    plan_id = kh.plan(target, 2)
    envelope = kh.apply(target, plan_id, 2)
    assert code(envelope) == "approval_required" and details(envelope) == {"status": "planned"}

    data = kh.call(REQUEST_APPROVAL, {"plan_id": plan_id})["data"]
    assert data["status"] == "awaiting_approval" and data["notified"] is True
    data = kh.call(REQUEST_APPROVAL, {"plan_id": plan_id})["data"]
    assert data["notified"] is False and data["retry_after"]

    for caller in (None, NOT_APPROVER, LAMBDA_SESSION):
        status, envelope = kh.approver("GET", caller, plan_id=plan_id)
        assert (status, code(envelope)) == (403, "forbidden"), caller
    status, envelope = decide(kh, plan_id, "approve", caller=LAMBDA_SESSION)
    assert status == 403
    assert kh.record(plan_id)["status"] == "awaiting_approval"

    status, envelope = kh.approver("GET", plan_id=plan_id)
    view = envelope["data"]
    assert status == 200 and view["status"] == "awaiting_approval"
    assert view["replicas"] == {"from": 1, "to": 2} and view["impact"]

    status, envelope = decide(kh, plan_id, "approve")
    applied = envelope["data"]["apply"]
    assert status == 200 and applied["ok"] is True, envelope
    assert kh.lambda_client.calls[-1]["custom"]["kubeHandrailChannel"] == "approval-api"
    assert kh.deployment(target["name"]).spec.replicas == 2
    assert kh.annotation(target["name"], LAST_PLAN_ANNOTATION) == plan_id
    status, envelope = kh.approver("GET", plan_id=plan_id)
    view = envelope["data"]
    assert view["status"] == "applied" and view["approval"]["approved_by"] == APPROVER
    ids = view["correlation_ids"]
    assert all(ids.get(step) for step in ("plan", "approval_request", "decision", "apply"))
    assert ids["decision"] == ids["apply"] == kh.annotation(target["name"], CORRELATION_ANNOTATION)

    status, envelope = decide(kh, plan_id, "approve")
    assert (status, code(envelope)) == (409, "plan_used")
    assert audit_findings(kh, plan_id) == []


def test_rejected_plan_cannot_be_applied(kh):
    kh.configure(APPROVAL_MODE="server")
    target = kh.create(deployment_manifest(unique("it-reject")))
    plan_id = kh.plan(target, 2)
    status, envelope = decide(kh, plan_id, "reject", reason="it rejection", apply=False)
    assert status == 200 and envelope["data"]["plan"]["status"] == "rejected"
    envelope = kh.apply(target, plan_id, 2)
    assert code(envelope) == "approval_required" and details(envelope) == {"status": "rejected"}
    status, envelope = decide(kh, plan_id, "approve")
    assert (status, code(envelope)) == (409, "plan_decided")


def test_both_mode_approval_does_not_apply(kh):
    kh.configure(APPROVAL_MODE="both")
    target = kh.create(deployment_manifest(unique("it-both")))
    plan_id = kh.plan(target, 2)
    kh.call(REQUEST_APPROVAL, {"plan_id": plan_id})
    status, envelope = decide(kh, plan_id, "approve")
    assert status == 200 and envelope["data"]["apply"] is None
    assert kh.deployment(target["name"]).spec.replicas == 1
    assert kh.apply(target, plan_id, 2)["ok"] is True
    assert audit_findings(kh, plan_id) == []


def test_approval_identity(kh):
    kh.configure(APPROVAL_MODE="server")
    target = kh.create(deployment_manifest(unique("it-approval-identity")))
    plan_id = kh.plan(target, 2)
    envelope = kh.call(REQUEST_APPROVAL, {"plan_id": plan_id}, client=CLIENT_B)
    assert code(envelope) == "forbidden" and details(envelope) == {"reason": "not_requester"}

    # B's plan, approved after B's kill switch was turned off: the approval's apply is
    # refused and the plan consumed.
    plan_b = kh.plan(target, 2, client=CLIENT_B)
    kh.call(REQUEST_APPROVAL, {"plan_id": plan_b}, client=CLIENT_B)
    kh.set_param(f"{AGENT_SWITCHES}{AGENT_B}/enabled", "false")
    status, envelope = decide(kh, plan_b, "approve")
    applied = envelope["data"]["apply"]
    assert code(applied) == "remediation_disabled" and details(applied)["agent"] == AGENT_B
    assert kh.record(plan_b)["used"] is True
    assert kh.deployment(target["name"]).spec.replicas == 1

    # Switch back on: an approved plan of B's applies and counts against B's budget.
    kh.set_param(f"{AGENT_SWITCHES}{AGENT_B}/enabled", "true")
    plan_b = kh.plan(target, 2, client=CLIENT_B)
    status, envelope = decide(kh, plan_b, "approve")
    assert envelope["data"]["apply"]["ok"] is True, envelope
    store = kh.write.get_runtime().store
    assert store.agent_applies(AGENT_B, kh.write_now()) == 1
