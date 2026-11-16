"""scripts/audit.py verify: fixture entries written by the audit Lambda into a moto bucket."""

import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location("audit_verify", _ROOT / "scripts" / "audit.py")
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)
_HSPEC = importlib.util.spec_from_file_location(
    "audit_handler_for_verify", _ROOT / "mcp" / "audit" / "handler.py"
)
handler = importlib.util.module_from_spec(_HSPEC)
_HSPEC.loader.exec_module(handler)

REGION = "us-east-1"
BUCKET = "audit-verify-test"
T0 = 1_700_000_000
_serializer = TypeSerializer()
_ids = iter(range(1, 1000))


def new_id() -> str:
    return f"00000000-0000-4000-8000-{next(_ids):012d}"


def planned(plan_id: str, mode: str | None) -> dict:
    plan = {
        "plan_id": plan_id,
        "action": "scale_deployment",
        "namespace": "sandbox",
        "name": "web",
        "args": {"namespace": "sandbox", "name": "web", "replicas": 3},
        "args_hash": "h-1",
        "replicas_to": 3,
        "requested_by": "agent:devops_agent",
        "agent": "devops_agent",
        "status": "planned",
        "used": False,
        "created_at": T0,
    }
    if mode is not None:
        plan["approval_mode"] = mode
    return plan


def approved(plan: dict, by: str = "approver-a") -> dict:
    return {
        **plan,
        "status": "approved",
        "approved_by": by,
        "approved_at": T0 + 10,
        "approved_args_hash": plan["args_hash"],
        "approval_channel": "api",
        "approval_expires_at": T0 + 310,
    }


def consumed(plan: dict, channel: str) -> dict:
    return {**plan, "used": True, "channel": channel, "verify_at": T0 + 140, "verify_queue": "due"}


def resulted(plan: dict, result: str = "applied") -> dict:
    return {**plan, "result": result, "applied_at": T0 + 21, "apply_correlation_id": "c-1"}


def verified(plan: dict) -> dict:
    done = {**plan, "verification": {"outcome": "healthy", "reason": "target_reached"}}
    done.pop("verify_queue", None)
    return done


def write(s3, plan_id: str, images: list[dict | None], start_seq: int = 100) -> None:
    """One stream record per consecutive pair of images (None: the item does not exist)."""
    records = []
    for offset, (old, new) in enumerate(zip(images, images[1:], strict=False)):
        name = "INSERT" if old is None else ("REMOVE" if new is None else "MODIFY")
        stream = {
            "Keys": {"plan_id": {"S": plan_id}},
            "SequenceNumber": str(start_seq + offset),
            "ApproximateCreationDateTime": T0 + offset * 10,
        }
        if old is not None:
            stream["OldImage"] = {k: _serializer.serialize(v) for k, v in old.items()}
        if new is not None:
            stream["NewImage"] = {k: _serializer.serialize(v) for k, v in new.items()}
        records.append({"eventID": str(start_seq + offset), "eventName": name, "dynamodb": stream})
    response, _ = handler.process(records, s3, BUCKET)
    assert response == {"batchItemFailures": []}


def server_chain(plan_id: str, mode: str = "server", channel: str = "approval-api") -> list:
    plan = planned(plan_id, mode)
    awaiting = {**plan, "status": "awaiting_approval", "approval_requested_at": T0 + 5}
    ok = approved(awaiting)
    used = consumed(ok, channel)
    result = resulted(used)
    return [None, plan, awaiting, ok, used, result, verified(result)]


def client_chain(plan_id: str, channel: str = "gateway") -> list:
    plan = planned(plan_id, "client")
    used = consumed(plan, channel)
    result = resulted(used)
    return [None, plan, used, result, verified(result)]


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, ObjectLockEnabledForBucket=True)
        yield client


def codes(report: dict) -> list[tuple[str, str]]:
    return [(f["severity"], f["code"]) for f in report["findings"]]


@pytest.mark.parametrize(("mode", "channel"), [("server", "approval-api"), ("server", "gateway")])
def test_a_server_mode_plan_applied_after_approval_has_no_findings(s3, mode, channel):
    plan_id = new_id()
    write(s3, plan_id, server_chain(plan_id, mode, channel))
    report = audit.verify_plan(s3, BUCKET, plan_id)
    assert report == {"plan_id": plan_id, "entries": 6, "ok": True, "findings": []}


def test_a_both_mode_plan_applied_through_the_gateway_has_no_findings(s3):
    plan_id = new_id()
    write(s3, plan_id, server_chain(plan_id, "both", "gateway"))
    assert audit.verify_plan(s3, BUCKET, plan_id)["findings"] == []


def test_an_unapplied_plan_that_expired_has_no_findings(s3):
    plan_id = new_id()
    plan = planned(plan_id, "server")
    write(s3, plan_id, [None, plan])
    ttl = {"type": "Service", "principalId": "dynamodb.amazonaws.com"}
    stream = {
        "Keys": {"plan_id": {"S": plan_id}},
        "SequenceNumber": "900",
        "ApproximateCreationDateTime": T0 + 900,
        "OldImage": {k: _serializer.serialize(v) for k, v in plan.items()},
    }
    expunge = {"eventID": "900", "eventName": "REMOVE", "dynamodb": stream, "userIdentity": ttl}
    handler.process([expunge], s3, BUCKET)
    assert audit.verify_plan(s3, BUCKET, plan_id)["findings"] == []


def test_a_client_mode_apply_is_client_unobserved_for_information(s3):
    plan_id = new_id()
    write(s3, plan_id, client_chain(plan_id))
    report = audit.verify_plan(s3, BUCKET, plan_id)
    assert report["ok"] is True
    assert codes(report) == [("info", "client_unobserved")]
    assert report["findings"][0]["details"]["approval_window"] == {"from": T0, "to": T0 + 10}


def test_a_client_mode_apply_with_a_recorded_approval_is_not_unobserved(s3):
    plan_id = new_id()
    plan = planned(plan_id, "client")
    ok = approved(plan)
    write(s3, plan_id, [None, plan, ok, consumed(ok, "gateway")])
    assert audit.verify_plan(s3, BUCKET, plan_id)["findings"] == []


@pytest.mark.parametrize(
    ("mode", "channel"),
    [("both", "approval-api"), ("client", "approval-api"), ("server", "verifier")],
)
def test_an_apply_on_the_wrong_channel_for_the_mode(s3, mode, channel):
    plan_id = new_id()
    chain = (
        client_chain(plan_id, channel) if mode == "client" else server_chain(plan_id, mode, channel)
    )
    write(s3, plan_id, chain)
    report = audit.verify_plan(s3, BUCKET, plan_id)
    assert ("finding", "wrong_channel") in codes(report)
    assert report["ok"] is False


@pytest.mark.parametrize("mode", ["server", "both"])
def test_an_apply_without_a_server_approval_in_server_or_both_mode(s3, mode):
    plan_id = new_id()
    plan = planned(plan_id, mode)
    used = consumed(plan, "gateway")
    write(s3, plan_id, [None, plan, used, resulted(used)])
    report = audit.verify_plan(s3, BUCKET, plan_id)
    assert codes(report)[0] == ("finding", "applied_without_approval")
    assert "no approved_by" in report["findings"][0]["message"]


def test_an_approval_by_the_requester_is_not_a_server_approval(s3):
    plan_id = new_id()
    plan = planned(plan_id, "server")
    ok = approved(plan, by="agent:devops_agent")
    write(s3, plan_id, [None, plan, ok, consumed(ok, "approval-api")])
    report = audit.verify_plan(s3, BUCKET, plan_id)
    assert "approved by the requester" in report["findings"][0]["message"]


def test_a_change_after_the_consume_is_the_executor_rewrite_pattern(s3):
    plan_id = new_id()
    chain = server_chain(plan_id)
    rewritten = {**chain[-1], "replicas_to": 5, "approved_by": "approver-b"}
    write(s3, plan_id, [*chain, rewritten])
    report = audit.verify_plan(s3, BUCKET, plan_id)
    assert codes(report) == [("finding", "executor_rewrite")]
    assert report["findings"][0]["details"]["changed"] == ["approved_by", "replicas_to"]


@pytest.mark.parametrize(
    "change",
    [
        lambda p: {**p, "used": False},
        lambda p: {**p, "result": "internal"},
        lambda p: {**p, "verification": {"outcome": "degraded", "reason": "x"}},
    ],
)
def test_a_flipped_use_or_a_second_result_or_verdict_is_a_rewrite(s3, change):
    plan_id = new_id()
    chain = server_chain(plan_id)
    write(s3, plan_id, [*chain, change(chain[-1])])
    assert ("finding", "executor_rewrite") in codes(audit.verify_plan(s3, BUCKET, plan_id))


def test_a_consume_that_changes_more_than_it_writes_is_a_rewrite(s3):
    plan_id = new_id()
    plan = planned(plan_id, "server")
    ok = approved(plan)
    forged = {**consumed(ok, "approval-api"), "replicas_to": 9, "revert_to": 1}
    write(s3, plan_id, [None, plan, ok, forged])
    report = audit.verify_plan(s3, BUCKET, plan_id)
    assert codes(report) == [("finding", "executor_rewrite")]
    assert report["findings"][0]["details"]["changed"] == ["replicas_to"]


def test_an_approval_rewritten_before_the_consume(s3):
    plan_id = new_id()
    plan = planned(plan_id, "server")
    ok = approved(plan)
    other = {**ok, "approved_by": "approver-b"}
    write(s3, plan_id, [None, plan, ok, other, consumed(other, "approval-api")])
    report = audit.verify_plan(s3, BUCKET, plan_id)
    assert codes(report) == [("finding", "approval_rewrite")]


def test_a_changed_approval_mode(s3):
    plan_id = new_id()
    plan = planned(plan_id, "server")
    relaxed = {**plan, "approval_mode": "client"}
    write(s3, plan_id, [None, plan, relaxed, consumed(relaxed, "gateway")])
    found = codes(audit.verify_plan(s3, BUCKET, plan_id))
    assert ("finding", "approval_mode_changed") in found


def test_a_gap_and_a_missing_creation(s3):
    plan_id = new_id()
    chain = server_chain(plan_id)
    # The creation and the approval request never reached the bucket.
    write(s3, plan_id, chain[2:4], start_seq=102)
    write(s3, plan_id, chain[4:6], start_seq=104)
    report = audit.verify_plan(s3, BUCKET, plan_id)
    assert ("finding", "missing_creation") in codes(report)
    assert ("finding", "gap") in codes(report)


def test_a_deleted_record(s3):
    plan_id = new_id()
    plan = planned(plan_id, "server")
    write(s3, plan_id, [None, plan, None])
    assert ("finding", "deleted") in codes(audit.verify_plan(s3, BUCKET, plan_id))


def test_a_record_without_approval_mode_is_reported_and_can_be_checked_with_a_default(s3):
    plan_id = new_id()
    chain = [None if image is None else dict(image) for image in server_chain(plan_id)]
    for image in chain[1:]:
        image.pop("approval_mode")
    write(s3, plan_id, chain)
    report = audit.verify_plan(s3, BUCKET, plan_id)
    assert report["ok"] is True and codes(report) == [("info", "approval_mode_unknown")]
    assert audit.verify_plan(s3, BUCKET, plan_id, "server")["findings"] == []
    assert codes(audit.verify_plan(s3, BUCKET, plan_id, "both")) == [("finding", "wrong_channel")]


def test_a_revert_record_runs_on_the_verifier_channel(s3):
    revert_id = new_id()
    revert = {
        **planned(revert_id, "server"),
        "kind": "revert",
        "status": "revert",
        "requested_by": "system:revert",
        "used": True,
        "channel": "verifier",
        "verify_queue": "due",
    }
    write(s3, revert_id, [None, revert, resulted(revert), verified(resulted(revert))])
    assert audit.verify_plan(s3, BUCKET, revert_id)["findings"] == []
    forged_id = new_id()
    write(s3, forged_id, [None, {**revert, "plan_id": forged_id, "channel": "gateway"}])
    assert codes(audit.verify_plan(s3, BUCKET, forged_id)) == [("finding", "wrong_channel")]


def test_no_entries_and_malformed_objects(s3):
    plan_id = new_id()
    assert codes(audit.verify_plan(s3, BUCKET, plan_id)) == [("finding", "no_entries")]
    write(s3, plan_id, server_chain(plan_id))
    s3.put_object(Bucket=BUCKET, Key=f"plans/{plan_id}/junk.json", Body=b"{}")
    assert codes(audit.verify_plan(s3, BUCKET, plan_id)) == [("finding", "malformed")]


def test_main_exit_status_and_json(s3, monkeypatch, capsys):
    monkeypatch.setenv("AUDIT_BUCKET", BUCKET)
    good, bad = new_id(), new_id()
    write(s3, good, server_chain(good))
    plan = planned(bad, "server")
    write(s3, bad, [None, plan, consumed(plan, "gateway")])
    assert audit.main(["verify", good], s3=s3) == 0
    assert f"{good}: ok" in capsys.readouterr().out
    assert audit.main(["verify", bad, "--json"], s3=s3) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False and report["findings"] == 1
    assert report["plans"][0]["findings"][0]["code"] == "applied_without_approval"


def test_main_verifies_every_plan_or_a_time_window(s3, monkeypatch, capsys):
    monkeypatch.setenv("AUDIT_BUCKET", BUCKET)
    first, second = new_id(), new_id()
    write(s3, first, server_chain(first))
    write(s3, second, client_chain(second))
    assert audit.main(["verify", "--all", "--json"], s3=s3) == 0
    report = json.loads(capsys.readouterr().out)
    # Oldest last entry first: the client chain has fewer entries, so it ends first.
    assert [p["plan_id"] for p in report["plans"]] == [second, first]
    # Entries are from 2023: none in the last hour.
    assert audit.main(["verify", "--since", "3600", "--json"], s3=s3) == 0
    assert json.loads(capsys.readouterr().out)["plans"] == []


def test_main_needs_exactly_one_selection(s3, monkeypatch):
    monkeypatch.setenv("AUDIT_BUCKET", BUCKET)
    with pytest.raises(SystemExit):
        audit.main(["verify"], s3=s3)
    with pytest.raises(SystemExit):
        audit.main(["verify", new_id(), "--all"], s3=s3)
