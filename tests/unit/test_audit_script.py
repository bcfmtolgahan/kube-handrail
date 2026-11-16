import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

_ROOT = Path(__file__).parents[2]
_SPEC = importlib.util.spec_from_file_location("audit_script", _ROOT / "scripts" / "audit.py")
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)
_HSPEC = importlib.util.spec_from_file_location(
    "audit_handler_for_script", _ROOT / "mcp" / "audit" / "handler.py"
)
handler = importlib.util.module_from_spec(_HSPEC)
_HSPEC.loader.exec_module(handler)

REGION = "us-east-1"
BUCKET = "audit-test"
PLAN_ID = "00000000-0000-4000-8000-000000000001"
REVERT_ID = "00000000-0000-4000-8000-000000000002"
OTHER_ID = "00000000-0000-4000-8000-000000000003"
_serializer = TypeSerializer()


def record(name, seq, plan_id, old=None, new=None, when=1_700_000_000, identity=None):
    stream = {
        "Keys": {"plan_id": {"S": plan_id}},
        "SequenceNumber": seq,
        "ApproximateCreationDateTime": when,
    }
    if old is not None:
        stream["OldImage"] = {k: _serializer.serialize(v) for k, v in old.items()}
    if new is not None:
        stream["NewImage"] = {k: _serializer.serialize(v) for k, v in new.items()}
    out = {"eventID": seq, "eventName": name, "dynamodb": stream}
    if identity:
        out["userIdentity"] = identity
    return out


PLANNED = {
    "plan_id": PLAN_ID,
    "action": "scale_deployment",
    "requested_by": "agent:devops_agent",
    "agent": "devops_agent",
    "status": "planned",
    "used": False,
}
AWAITING = {**PLANNED, "status": "awaiting_approval"}
APPROVED = {**AWAITING, "status": "approved", "approved_by": "approver-a"}
APPLIED = {**APPROVED, "used": True, "channel": "approval-api"}
RESULT = {**APPLIED, "result": "applied", "applied_at": 1_700_000_030}
VERIFIED = {
    **RESULT,
    "verification": {"outcome": "degraded", "reason": "replica_failure"},
    "revert": {"status": "reverted", "reason": None, "revert_plan_id": REVERT_ID},
}
REVERT = {
    "plan_id": REVERT_ID,
    "kind": "revert",
    "status": "revert",
    "requested_by": "system:revert",
    "reverts_plan_id": PLAN_ID,
    "used": True,
    "result": "applied",
}


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, ObjectLockEnabledForBucket=True)
        records = [
            record("INSERT", "100", PLAN_ID, new=PLANNED),
            record("MODIFY", "101", PLAN_ID, PLANNED, AWAITING, when=1_700_000_005),
            record("MODIFY", "102", PLAN_ID, AWAITING, APPROVED, when=1_700_000_010),
            record("MODIFY", "103", PLAN_ID, APPROVED, APPLIED, when=1_700_000_020),
            record("MODIFY", "104", PLAN_ID, APPLIED, RESULT, when=1_700_000_030),
            record("INSERT", "105", REVERT_ID, new=REVERT, when=1_700_000_100),
            record("MODIFY", "106", PLAN_ID, RESULT, VERIFIED, when=1_700_000_101),
            record("INSERT", "107", OTHER_ID, new={**PLANNED, "plan_id": OTHER_ID}, when=5),
        ]
        response, _ = handler.process(records, client, BUCKET)
        assert response == {"batchItemFailures": []}
        yield client


def test_export_summary_names_the_chain(s3):
    bundle = audit.export(s3, BUCKET, PLAN_ID)
    summary = bundle["summary"]
    assert summary["entries"] == 6
    assert summary["requested_by"] == "agent:devops_agent"
    assert summary["agent"] == "devops_agent"
    assert summary["approved_by"] == ["approver-a"]
    assert summary["channel"] == "approval-api"
    assert summary["result"] == "applied"
    assert summary["status_history"] == ["planned", "awaiting_approval", "approved"]
    assert summary["verification"] == {"outcome": "degraded", "reason": "replica_failure"}
    assert summary["revert"]["revert_plan_id"] == REVERT_ID
    assert summary["has_creation"] is True
    assert summary["removed"] is None
    assert "k8s_write" in summary["executed_by"]["attribution"]
    seqs = [e["stream"]["sequence_number"] for e in bundle["entries"]]
    assert seqs == ["100", "101", "102", "103", "104", "106"]


def test_export_follows_the_revert(s3):
    bundle = audit.export(s3, BUCKET, PLAN_ID)
    assert [(link["relation"], link["plan_id"]) for link in bundle["linked"]] == [
        ("revert", REVERT_ID)
    ]
    linked = bundle["linked"][0]
    assert linked["summary"]["requested_by"] == "system:revert"
    assert len(linked["entries"]) == 1


def test_export_of_a_revert_links_back(s3):
    bundle = audit.export(s3, BUCKET, REVERT_ID)
    assert [(link["relation"], link["plan_id"]) for link in bundle["linked"]] == [
        ("reverts", PLAN_ID)
    ]
    assert bundle["summary"]["kind"] == "revert"


def test_no_links(s3):
    assert audit.export(s3, BUCKET, PLAN_ID, follow_links=False)["linked"] == []


def test_expunged_plan_still_exports(s3):
    ttl = {"type": "Service", "principalId": "dynamodb.amazonaws.com"}
    handler.process(
        [record("REMOVE", "200", OTHER_ID, old={**PLANNED}, when=9, identity=ttl)], s3, BUCKET
    )
    summary = audit.export(s3, BUCKET, OTHER_ID)["summary"]
    assert summary["removed"] == "expunged"
    assert summary["requested_by"] == "agent:devops_agent"


def test_list_newest_first(s3):
    plans = audit.list_plans(s3, BUCKET, 10)
    assert [p["plan_id"] for p in plans] == [PLAN_ID, REVERT_ID, OTHER_ID]
    assert plans[0]["entries"] == 6


def test_main_writes_bundle(s3, tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_BUCKET", BUCKET)
    out = tmp_path / "bundle.json"
    assert audit.main(["export", PLAN_ID, "--out", str(out)], s3=s3) == 0
    assert json.loads(out.read_text())["summary"]["result"] == "applied"


def test_main_unknown_plan_fails(s3, monkeypatch, capsys):
    monkeypatch.setenv("AUDIT_BUCKET", BUCKET)
    missing = "00000000-0000-4000-8000-0000000000ff"
    assert audit.main(["export", missing], s3=s3) == 1
    assert "no audit entries" in capsys.readouterr().err


def test_plan_id_is_validated():
    with pytest.raises(SystemExit):
        audit.main(["--bucket", BUCKET, "export", "../etc"], s3=object())


def test_objects_the_lambda_did_not_write_are_flagged(s3):
    prefix = f"plans/{PLAN_ID}/"
    s3.put_object(Bucket=BUCKET, Key=prefix + "0000000001-junk.json", Body=b"{}")
    s3.put_object(Bucket=BUCKET, Key=prefix + "not-json.json", Body=b"\x00")
    s3.put_object(Bucket=BUCKET, Key=prefix + "x.json", Body=b"[]")
    bundle = audit.export(s3, BUCKET, PLAN_ID)
    assert bundle["summary"]["entries"] == 6
    assert bundle["summary"]["result"] == "applied"
    assert bundle["summary"]["malformed"] == 3
    assert sorted(bundle["malformed"]) == sorted(
        [prefix + "0000000001-junk.json", prefix + "not-json.json", prefix + "x.json"]
    )
    plans = audit.list_plans(s3, BUCKET, 10)
    assert plans[0]["plan_id"] == PLAN_ID


def test_undecoded_entries_do_not_feed_the_summary(s3, monkeypatch):
    monkeypatch.setattr(handler, "summarize", lambda *_: (_ for _ in ()).throw(TypeError()))
    forged = {**VERIFIED, "approved_by": "someone-else"}
    handler.process(
        [record("MODIFY", "300", PLAN_ID, VERIFIED, forged, when=1_700_000_200)], s3, BUCKET
    )
    summary = audit.export(s3, BUCKET, PLAN_ID)["summary"]
    assert summary["entries"] == 7
    assert summary["undecoded"] == 1
    assert summary["approved_by"] == ["approver-a"]


def test_export_gives_query_templates_for_the_executor(s3):
    executed = audit.export(s3, BUCKET, PLAN_ID)["summary"]["executed_by"]
    lambda_logs = executed["queries"]["lambda_logs"]
    # The bucket name does not follow <name_prefix>-audit-<account id>: a placeholder.
    assert "/aws/lambda/<name_prefix>-k8s-write" in lambda_logs["log_groups"]
    assert f'@message like "{PLAN_ID}"' in lambda_logs["query"]
    assert (lambda_logs["start"], lambda_logs["end"]) == (1_700_000_000 - 300, 1_700_000_101 + 300)
    eks = executed["queries"]["eks_audit_log"]
    assert eks["log_groups"] == ["/aws/eks/<cluster_name>/cluster"]
    assert f'userAgent like "plan/{PLAN_ID}"' in eks["query"]


def test_queries_use_the_correlation_ids_name_prefix_and_cluster():
    image = {
        "plan_correlation_id": "plan-1",
        "decision_correlation_id": "dec-1",
        "apply_correlation_id": "dec-1",
        "verification": {"correlation_id": "ver-1"},
        "cluster": "demo",
    }
    found = audit.queries(PLAN_ID, "kh-test-audit-000000000000", {}, image)
    assert found["lambda_logs"]["log_groups"][1] == "/aws/lambda/kh-test-k8s-write"
    assert 'correlation_id in ["plan-1", "dec-1", "ver-1"]' in found["lambda_logs"]["query"]
    assert "start" not in found["lambda_logs"]
    assert found["eks_audit_log"]["log_groups"] == ["/aws/eks/demo/cluster"]


def test_an_unconsumed_plan_has_no_executor(s3):
    assert "executed_by" not in audit.export(s3, BUCKET, OTHER_ID)["summary"]
