import uuid

import boto3
import pytest
from moto import mock_aws

from handrail_common.envelope import PLAN_EXPIRED, PLAN_MISMATCH, PLAN_NOT_FOUND, PLAN_USED
from handrail_common.plans import SCALE_DEPLOYMENT, Plan, PlanStore, args_hash, scale_args_hash
from handrail_common.validation import GuardError

NOW = 1_700_000_000
TABLE = "plans-test"


def test_args_hash_is_order_independent():
    a = {"action": "scale_deployment", "namespace": "sandbox", "name": "web", "replicas": 2}
    b = dict(reversed(list(a.items())))
    assert args_hash(a) == args_hash(b) == scale_args_hash("sandbox", "web", 2)
    assert len(args_hash(a)) == 64


def test_args_hash_distinguishes_values():
    assert scale_args_hash("sandbox", "web", 2) != scale_args_hash("sandbox", "web", 3)
    # Integer and string replicas must not collide.
    assert args_hash({"replicas": 2}) != args_hash({"replicas": "2"})


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
        dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "plan_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "plan_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield PlanStore.from_name(TABLE, "us-east-1")


def make_plan(expires_at: int = NOW + 300) -> Plan:
    return Plan(
        plan_id=str(uuid.uuid4()),
        action=SCALE_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=1,
        replicas_to=3,
        args_hash=scale_args_hash("sandbox", "web", 3),
        resource_version="12345",
        created_at=NOW,
        expires_at=expires_at,
    )


def consume_code(store: PlanStore, plan_id: str, h: str, action: str = SCALE_DEPLOYMENT) -> str:
    with pytest.raises(GuardError) as exc:
        store.consume(plan_id, h, action, NOW)
    return exc.value.code


def test_consume_success_then_used(store):
    plan = make_plan()
    store.create(plan)
    consumed = store.consume(plan.plan_id, plan.args_hash, SCALE_DEPLOYMENT, NOW)
    assert consumed.used is True
    assert consumed.resource_version == "12345"
    assert consumed.replicas_to == 3
    assert consume_code(store, plan.plan_id, plan.args_hash) == PLAN_USED


def test_consume_not_found(store):
    assert consume_code(store, str(uuid.uuid4()), "x") == PLAN_NOT_FOUND


def test_consume_expired(store):
    plan = make_plan(expires_at=NOW)
    store.create(plan)
    assert consume_code(store, plan.plan_id, plan.args_hash) == PLAN_EXPIRED


def test_consume_hash_mismatch_leaves_plan_usable(store):
    plan = make_plan()
    store.create(plan)
    other = scale_args_hash("sandbox", "web", 4)
    assert consume_code(store, plan.plan_id, other) == PLAN_MISMATCH
    assert store.consume(plan.plan_id, plan.args_hash, SCALE_DEPLOYMENT, NOW).used


def test_consume_action_mismatch(store):
    plan = make_plan()
    store.create(plan)
    assert consume_code(store, plan.plan_id, plan.args_hash, "restart") == PLAN_MISMATCH


def test_create_refuses_duplicate_id(store):
    plan = make_plan()
    store.create(plan)
    with pytest.raises(Exception):
        store.create(plan)


def test_record_result(store):
    plan = make_plan()
    store.create(plan)
    store.consume(plan.plan_id, plan.args_hash, SCALE_DEPLOYMENT, NOW)
    store.record_result(plan.plan_id, NOW + 1, "applied")
    item = boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE).get_item(
        Key={"plan_id": plan.plan_id}
    )["Item"]
    assert item["result"] == "applied" and item["applied_at"] == NOW + 1
