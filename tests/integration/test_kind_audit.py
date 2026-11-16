"""The durable audit record: the plan table's stream, as the event source mapping delivers
it, through the audit Lambda into the Object Lock bucket (tests/e2e/e2e_gateway.py:
audit_tests). The records are read from moto's stream and handed to the Lambda by hand."""

import json

import pytest
from botocore.exceptions import ClientError
from harness import AGENT_A, BUCKET, deployment_manifest, load_script, stream_records, unique


def test_plan_and_apply_reach_the_bucket_once(kh):
    target = kh.create(deployment_manifest(unique("it-audit")))
    plan_id = kh.plan(target, 2)
    assert kh.apply(target, plan_id, 2)["ok"] is True

    records = stream_records(kh.stream_arn())
    response = kh.run_audit(records)
    assert response == {"batchItemFailures": []}
    keys = [
        o["Key"]
        for o in kh.s3.list_objects_v2(Bucket=BUCKET, Prefix=f"plans/{plan_id}/")["Contents"]
    ]
    entries = [json.loads(kh.s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()) for k in keys]
    assert [e["event"] for e in entries][0] == "created" and len(entries) >= 2
    assert entries[-1]["new_image"]["used"] is True
    # Counter items share the table and are skipped, not recorded.
    budget = kh.s3.list_objects_v2(Bucket=BUCKET, Prefix="budget")
    assert budget.get("KeyCount", 0) == 0

    audit = load_script("audit")
    bundle = audit.export(kh.s3, BUCKET, plan_id)
    summary = bundle["summary"]
    assert summary["agent"] == AGENT_A and summary["requested_by"] == f"agent:{AGENT_A}"
    assert summary["result"] == "applied" and summary["has_creation"] is True

    # A redelivered batch maps to the same keys: duplicates, no failure, nothing new.
    assert kh.run_audit(records) == {"batchItemFailures": []}
    again = kh.s3.list_objects_v2(Bucket=BUCKET, Prefix=f"plans/{plan_id}/")["KeyCount"]
    assert again == len(keys)

    # Every entry is under the bucket's default retention and cannot be overwritten.
    head = kh.s3.head_object(Bucket=BUCKET, Key=keys[0])
    assert head["ObjectLockMode"] == "GOVERNANCE" and head["ObjectLockRetainUntilDate"]
    with pytest.raises(ClientError) as error:
        kh.s3.put_object(Bucket=BUCKET, Key=keys[0], Body=b"{}", IfNoneMatch="*")
    assert error.value.response["Error"]["Code"] == "PreconditionFailed"

    # A different entry under an existing key stops the batch at that record.
    tampered = json.loads(json.dumps(records))
    first = next(r for r in tampered if r["eventName"] == "INSERT" and plan_id in json.dumps(r))
    first["dynamodb"]["NewImage"]["name"] = {"S": "changed"}
    response = kh.run_audit([first])
    assert response == {
        "batchItemFailures": [{"itemIdentifier": first["dynamodb"]["SequenceNumber"]}]
    }


def test_the_api_server_sees_the_user_agent(kh):
    """The API server derives a patch's default field manager from the User-Agent's product
    (before the first `/`), so the Deployment's managed fields show that the header reached
    it. The full header (plan and correlation id) is only in the API server's audit log,
    which the kind cluster of this suite does not enable."""
    target = kh.create(deployment_manifest(unique("it-agent")))
    plan_id = kh.plan(target, 2)
    assert kh.apply(target, plan_id, 2)["ok"] is True
    managers = {
        (entry.manager, entry.operation)
        for entry in kh.deployment(target["name"]).metadata.managed_fields or []
    }
    assert ("kube-handrail", "Update") in managers
