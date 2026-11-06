import importlib.util
import json
from pathlib import Path

import boto3
import pytest
from boto3.dynamodb.types import TypeSerializer
from moto import mock_aws

from handrail_common.config import ConfigError

# Load by path: every Lambda directory contains a module named `handler`.
_SPEC = importlib.util.spec_from_file_location(
    "audit_handler", Path(__file__).parents[2] / "mcp" / "audit" / "handler.py"
)
handler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(handler)

REGION = "us-east-1"
BUCKET = "audit-test"
PLAN_ID = "00000000-0000-4000-8000-000000000001"
_serializer = TypeSerializer()


def ddb(item: dict) -> dict:
    return {k: _serializer.serialize(v) for k, v in item.items()}


def stream_record(
    name: str,
    seq: str,
    old: dict | None = None,
    new: dict | None = None,
    plan_id: str = PLAN_ID,
    when: int = 1_700_000_000,
    identity: dict | None = None,
) -> dict:
    stream = {
        "Keys": ddb({"plan_id": plan_id}),
        "SequenceNumber": seq,
        "ApproximateCreationDateTime": when,
        "StreamViewType": "NEW_AND_OLD_IMAGES",
    }
    if old is not None:
        stream["OldImage"] = ddb(old)
    if new is not None:
        stream["NewImage"] = ddb(new)
    record = {"eventID": f"e-{seq}", "eventName": name, "dynamodb": stream}
    if identity is not None:
        record["userIdentity"] = identity
    return record


PLANNED = {
    "plan_id": PLAN_ID,
    "action": "scale_deployment",
    "namespace": "sandbox",
    "name": "web",
    "requested_by": "agent:devops_agent",
    "agent": "devops_agent",
    "status": "planned",
    "used": False,
    "replicas_to": 3,
}
APPROVED = {**PLANNED, "status": "approved", "approved_by": "approver-a"}
APPLIED = {**APPROVED, "used": True, "channel": "approval-api", "result": "applied"}
TTL_IDENTITY = {"type": "Service", "principalId": "dynamodb.amazonaws.com"}


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=BUCKET, ObjectLockEnabledForBucket=True)
        yield client


def keys(s3) -> list[str]:
    return [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", [])]


def stored(s3, key: str) -> dict:
    return json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())


def test_settings():
    settings = handler.load_settings({"AUDIT_BUCKET": "b", "AWS_REGION": REGION})
    assert settings.bucket == "b"
    assert settings.metric_namespace == handler.DEFAULT_METRIC_NAMESPACE
    with pytest.raises(ConfigError):
        handler.load_settings({})


def test_plain_values():
    from decimal import Decimal

    assert handler.plain(Decimal("3")) == 3
    assert handler.plain(Decimal("1.5")) == "1.5"
    assert handler.plain({"a": {Decimal("2"), Decimal("1")}}) == {"a": [1, 2]}
    assert handler.plain(b"\x00\x01") == "AAE="


def test_insert_entry():
    key, entry = handler.build_entry(stream_record("INSERT", "100", new=PLANNED))
    assert key == f"plans/{PLAN_ID}/1700000000-{'100'.zfill(40)}.json"
    assert entry["event"] == "created"
    assert entry["old_image"] is None
    assert entry["new_image"]["replicas_to"] == 3
    assert entry["stream"]["event_id"] == "e-100"
    assert entry["stream"]["ttl_removal"] is False
    summary = entry["summary"]
    assert summary["requested_by"] == "agent:devops_agent"
    assert summary["status"] == {"from": None, "to": "planned"}
    assert summary["used"] == {"from": None, "to": False}


def test_modify_summary_transitions():
    _, entry = handler.build_entry(stream_record("MODIFY", "101", old=APPROVED, new=APPLIED))
    summary = entry["summary"]
    assert entry["event"] == "modified"
    assert summary["used"] == {"from": False, "to": True}
    assert summary["result"] == {"from": None, "to": "applied"}
    assert summary["status"] == "approved"
    assert summary["approved_by"] == "approver-a"
    assert summary["channel"] == "approval-api"
    assert summary["changed"] == ["channel", "result", "used"]


def test_verification_and_revert_in_summary():
    old = {**APPLIED}
    new = {
        **APPLIED,
        "verification": {"outcome": "degraded", "reason": "replica_failure"},
        "revert": {"status": "reverted", "revert_plan_id": "r-1"},
    }
    _, entry = handler.build_entry(stream_record("MODIFY", "102", old=old, new=new))
    assert entry["summary"]["verification"] == "degraded"
    assert entry["summary"]["revert"] == {"status": "reverted", "revert_plan_id": "r-1"}


def test_remove_by_ttl_is_expunged():
    _, entry = handler.build_entry(
        stream_record("REMOVE", "103", old=APPLIED, identity=TTL_IDENTITY)
    )
    assert entry["event"] == "expunged"
    assert entry["stream"]["ttl_removal"] is True
    assert entry["new_image"] is None
    assert entry["summary"]["requested_by"] == "agent:devops_agent"


def test_remove_by_anyone_else_is_deleted():
    _, plain_delete = handler.build_entry(stream_record("REMOVE", "104", old=APPLIED))
    assert plain_delete["event"] == "deleted"
    assert plain_delete["stream"]["ttl_removal"] is False
    other = {"type": "Service", "principalId": "example.amazonaws.com"}
    _, spoof = handler.build_entry(stream_record("REMOVE", "105", old=APPLIED, identity=other))
    assert spoof["event"] == "deleted"


def test_key_and_bytes_are_deterministic():
    record = stream_record("MODIFY", "106", old=APPROVED, new=APPLIED)
    first_key, first = handler.build_entry(record)
    again_key, again = handler.build_entry(json.loads(json.dumps(record)))
    assert first_key == again_key
    assert handler.canonical(first) == handler.canonical(again)


def test_keys_list_in_event_order():
    short = handler.object_key(PLAN_ID, 1_700_000_000, "9" * 21)
    long = handler.object_key(PLAN_ID, 1_700_000_000, "1" + "0" * 21)
    later = handler.object_key(PLAN_ID, 1_700_000_001, "1" * 21)
    assert sorted([later, long, short]) == [short, long, later]


def test_bad_sequence_number_is_refused():
    with pytest.raises(ValueError):
        handler.object_key(PLAN_ID, 1, "12a")


def test_process_writes_one_object_per_record(s3):
    records = [
        stream_record("INSERT", "200", new=PLANNED),
        stream_record("MODIFY", "201", old=PLANNED, new=APPROVED, when=1_700_000_010),
        stream_record("MODIFY", "202", old=APPROVED, new=APPLIED, when=1_700_000_020),
    ]
    response, counts = handler.process(records, s3, BUCKET)
    assert response == {"batchItemFailures": []}
    assert counts.written == 3
    listed = keys(s3)
    assert len(listed) == 3
    assert [stored(s3, k)["stream"]["sequence_number"] for k in listed] == ["200", "201", "202"]


def test_counters_are_skipped(s3):
    records = [
        stream_record(
            "MODIFY", "300", old={"applies": 1}, new={"applies": 2}, plan_id="budget#global#1"
        ),
        stream_record("INSERT", "301", new=PLANNED),
    ]
    response, counts = handler.process(records, s3, BUCKET)
    assert response == {"batchItemFailures": []}
    assert counts.skipped == 1 and counts.written == 1
    assert len(keys(s3)) == 1


def test_redelivery_with_same_bytes_is_success(s3):
    record = stream_record("INSERT", "400", new=PLANNED)
    handler.process([record], s3, BUCKET)
    response, counts = handler.process([record], s3, BUCKET)
    assert response == {"batchItemFailures": []}
    assert counts.duplicates == 1 and counts.written == 0
    versions = s3.list_object_versions(Bucket=BUCKET).get("Versions", [])
    assert len(versions) == 1


def test_existing_object_with_other_bytes_fails_the_record(s3):
    record = stream_record("INSERT", "500", new=PLANNED)
    key, _ = handler.build_entry(record)
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"{}")
    response, counts = handler.process([record], s3, BUCKET)
    assert response == {"batchItemFailures": [{"itemIdentifier": "500"}]}
    assert counts.mismatches == 1 and counts.failures == 1
    assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == b"{}"


class FlakyS3:
    """Fails put_object for one key; otherwise delegates."""

    def __init__(self, inner, fail_seq: str) -> None:
        self.inner = inner
        self.fail_seq = fail_seq
        self.puts: list[str] = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs["Key"])
        if kwargs["Key"].endswith(f"{self.fail_seq.zfill(40)}.json"):
            raise RuntimeError("throttled")
        return self.inner.put_object(**kwargs)

    def get_object(self, **kwargs):
        return self.inner.get_object(**kwargs)


def test_first_failure_stops_the_batch(s3):
    records = [
        stream_record("INSERT", "600", new=PLANNED),
        stream_record("MODIFY", "601", old=PLANNED, new=APPROVED),
        stream_record("MODIFY", "602", old=APPROVED, new=APPLIED),
    ]
    flaky = FlakyS3(s3, "601")
    response, counts = handler.process(records, flaky, BUCKET)
    # Only the failed record is reported; the event source mapping retries from it, so the
    # record after it is not written out of order.
    assert response == {"batchItemFailures": [{"itemIdentifier": "601"}]}
    assert len(flaky.puts) == 2
    assert counts.written == 1 and counts.failures == 1
    assert len(keys(s3)) == 1
    # The retry from 601 writes the rest; 600 is not part of it.
    response, counts = handler.process(records[1:], s3, BUCKET)
    assert response == {"batchItemFailures": []}
    assert len(keys(s3)) == 3


def test_unknown_event_name_fails_the_record(s3):
    record = stream_record("INSERT", "700", new=PLANNED)
    record["eventName"] = "UPSERT"
    response, _ = handler.process([record], s3, BUCKET)
    assert response == {"batchItemFailures": [{"itemIdentifier": "700"}]}


def test_emit_logs_counts_only(capsys):
    counts = handler.Counts(records=2, written=1, failures=1, failed=["9"])
    handler.emit(counts, "ns/audit", "req-1")
    line = json.loads(capsys.readouterr().out)
    assert line["Records"] == 2 and line["Failures"] == 1
    assert line["failed_sequence_numbers"] == ["9"]
    assert line["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "ns/audit"


def test_binary_values_in_the_lambda_event_shape_are_recorded(s3):
    # Lambda delivers stream binary values as base64 strings, not bytes.
    record = stream_record("MODIFY", "800", old=APPROVED, new=APPLIED)
    record["dynamodb"]["OldImage"]["x"] = {"B": "AA=="}
    record["dynamodb"]["NewImage"]["x"] = {"BS": ["AQ==", "AA=="]}
    record["dynamodb"]["NewImage"]["m"] = {"M": {"b": {"B": "Ag=="}}}
    response, counts = handler.process([record], s3, BUCKET)
    assert response == {"batchItemFailures": []}
    entry = stored(s3, keys(s3)[0])
    assert entry["old_image"]["x"] == "AA=="
    assert entry["new_image"]["x"] == ["AA==", "AQ=="]
    assert entry["new_image"]["m"] == {"b": "Ag=="}
    assert entry["summary"]["result"] == {"from": None, "to": "applied"}


def test_an_image_that_cannot_be_decoded_is_stored_raw(s3, monkeypatch):
    def broken(*_args):
        raise TypeError("cannot decode")

    monkeypatch.setattr(handler, "summarize", broken)
    record = stream_record("MODIFY", "801", old=APPROVED, new=APPLIED)
    response, counts = handler.process([record], s3, BUCKET)
    # Only S3 errors fail a record; the raw images are kept instead.
    assert response == {"batchItemFailures": []}
    entry = stored(s3, keys(s3)[0])
    assert entry["image_format"] == "dynamodb"
    assert entry["new_image"] == record["dynamodb"]["NewImage"]
    assert entry["old_image"] == record["dynamodb"]["OldImage"]
    assert entry["summary"] == {"plan_id": PLAN_ID, "error": "TypeError"}
