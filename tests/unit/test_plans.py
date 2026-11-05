import dataclasses
import uuid

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from handrail_common.envelope import PLAN_EXPIRED, PLAN_MISMATCH, PLAN_NOT_FOUND, PLAN_USED
from handrail_common.plans import (
    RESTART_ANNOTATION,
    RESTART_DEPLOYMENT,
    SCALE_DEPLOYMENT,
    Plan,
    PlanStore,
    args_hash,
    global_budget_key,
    object_key,
    plan_args_hash,
    restart_args,
    restart_template,
    scale_args,
    scale_args_hash,
    template_hash,
)
from handrail_common.validation import GuardError, require_plan_id

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


def test_override_enters_the_hash_only_when_true():
    plain = scale_args_hash("sandbox", "web", 2)
    # Hashes for plans without the override are unchanged by its introduction.
    assert plain == args_hash(
        {"action": "scale_deployment", "namespace": "sandbox", "name": "web", "replicas": 2}
    )
    assert scale_args_hash("sandbox", "web", 2, False) == plain
    assert scale_args_hash("sandbox", "web", 2, True) == args_hash(
        {
            "action": "scale_deployment",
            "namespace": "sandbox",
            "name": "web",
            "replicas": 2,
            "override_manual_change": True,
        }
    )
    assert scale_args_hash("sandbox", "web", 2, True) != plain


def test_args_hash_covers_the_stored_payload_and_the_action():
    args = scale_args("sandbox", "web", 2, True)
    assert args == {
        "namespace": "sandbox",
        "name": "web",
        "replicas": 2,
        "override_manual_change": True,
    }
    assert "override_manual_change" not in scale_args("sandbox", "web", 2)
    assert plan_args_hash(SCALE_DEPLOYMENT, args) == scale_args_hash("sandbox", "web", 2, True)
    assert plan_args_hash("restart_deployment", args) != plan_args_hash(SCALE_DEPLOYMENT, args)


def test_template_hash_is_stable_and_changes_with_any_value():
    template = {"metadata": {"labels": {"app": "web"}}, "spec": {"containers": [{"name": "a"}]}}
    reordered = {"spec": template["spec"], "metadata": template["metadata"]}
    digest = template_hash(template)
    assert digest == template_hash(reordered)
    assert digest.startswith("sha256:") and len(digest) == len("sha256:") + 64
    assert template_hash({**template, "spec": {"containers": [{"name": "b"}]}}) != digest


@pytest.fixture
def store(monkeypatch, plans_table):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        plans_table(TABLE, "us-east-1")
        yield PlanStore.from_name(TABLE, "us-east-1")


UID = "8f7c1a2e-0000-4000-8000-000000000001"


def make_plan(
    expires_at: int = NOW + 300, created_at: int = NOW, uid: str = UID, name: str = "web"
) -> Plan:
    return Plan(
        plan_id=str(uuid.uuid4()),
        action=SCALE_DEPLOYMENT,
        namespace="sandbox",
        name=name,
        replicas_from=1,
        replicas_to=3,
        args_hash=scale_args_hash("sandbox", name, 3),
        resource_version="12345",
        generation=4,
        uid=uid,
        created_at=created_at,
        expires_at=expires_at,
        retain_until=created_at + 4000,
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
    assert consumed.generation == 4
    assert consumed.uid == "8f7c1a2e-0000-4000-8000-000000000001"
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
    with pytest.raises(ClientError, match="ConditionalCheckFailed"):
        store.create(plan)


def test_record_result(store):
    plan = make_plan()
    store.create(plan)
    store.consume(plan.plan_id, plan.args_hash, SCALE_DEPLOYMENT, NOW)
    store.record_result(plan.plan_id, NOW + 1, "applied")
    item = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(TABLE)
        .get_item(Key={"plan_id": plan.plan_id})["Item"]
    )
    assert item["result"] == "applied" and item["applied_at"] == NOW + 1
    assert "apply_correlation_id" not in item


def test_record_result_with_a_correlation_id(store):
    plan = make_plan()
    store.create(plan)
    store.record_result(plan.plan_id, NOW + 1, "conflict", "req-1")
    item = store.get(plan.plan_id)
    assert (item["result"], item["apply_correlation_id"]) == ("conflict", "req-1")


def test_correlation_ids_round_trip():
    plan = dataclasses.replace(
        make_plan(),
        plan_correlation_id="a",
        approval_request_correlation_id="b",
        decision_correlation_id="c",
        apply_correlation_id="d",
    )
    item = plan.to_item()
    steps = ("plan", "approval_request", "decision", "apply")
    assert [item[f"{step}_correlation_id"] for step in steps] == ["a", "b", "c", "d"]
    assert Plan.from_item(item) == plan


def test_args_and_template_hashes_round_trip(store):
    plan = dataclasses.replace(
        make_plan(),
        args=scale_args("sandbox", "web", 3),
        template_from="sha256:a",
        template_to="sha256:a",
    )
    store.create(plan)
    # DynamoDB returns the numbers in the payload as Decimal.
    restored = Plan.from_item(store.get(plan.plan_id))
    assert restored == plan
    assert type(restored.args["replicas"]) is int


def test_restart_template_sets_only_the_annotation_on_a_copy():
    template = {"metadata": {"labels": {"app": "web"}, "annotations": {"a": "1"}}, "spec": {}}
    restarted = restart_template(template, "p-1")
    assert restarted == {
        "metadata": {
            "labels": {"app": "web"},
            "annotations": {"a": "1", RESTART_ANNOTATION: "p-1"},
        },
        "spec": {},
    }
    assert template["metadata"]["annotations"] == {"a": "1"}
    assert restart_template({}, "p-1") == {"metadata": {"annotations": {RESTART_ANNOTATION: "p-1"}}}
    # Unique per plan, so no two restarts leave the same template.
    assert template_hash(restart_template(template, "p-2")) != template_hash(restarted)


def test_restart_record_round_trips(store):
    plan = dataclasses.replace(
        make_plan(),
        action=RESTART_DEPLOYMENT,
        replicas_to=1,
        args_hash=plan_args_hash(RESTART_DEPLOYMENT, restart_args("sandbox", "web")),
        args=restart_args("sandbox", "web"),
        template_from="sha256:a",
        template_to="sha256:b",
        revision_from=7,
    )
    store.create(plan)
    restored = Plan.from_item(store.get(plan.plan_id))
    assert restored == plan
    assert type(restored.revision_from) is int
    assert "revision_from" not in make_plan().to_item()


def test_from_item_without_args_has_none():
    plan = Plan.from_item(make_plan().to_item())
    assert plan.args is None and plan.template_from is None and plan.template_to is None


def test_from_item_without_generation_or_uid_never_matches():
    item = make_plan().to_item()
    del item["generation"]
    del item["uid"]
    plan = Plan.from_item(item)
    assert plan.generation == -1
    assert plan.uid == ""


def test_item_carries_object_key_and_retention():
    item = make_plan().to_item()
    assert item["object_key"] == f"sandbox/web/{UID}" == object_key("sandbox", "web", UID)
    assert item["retain_until"] == NOW + 4000
    assert Plan.from_item(item).retain_until == NOW + 4000


def test_history_returns_one_object_from_since_oldest_first(store):
    other_uid = "8f7c1a2e-0000-4000-8000-000000000002"
    plans = [make_plan(created_at=NOW - age) for age in (50, 10, 100, 200)]
    for plan in plans:
        store.create(plan)
    store.create(make_plan(created_at=NOW - 20, uid=other_uid))  # recreated object
    store.create(make_plan(created_at=NOW - 20, name="api"))  # another Deployment
    store.consume(plans[0].plan_id, plans[0].args_hash, SCALE_DEPLOYMENT, NOW)
    store.record_result(plans[0].plan_id, NOW - 40, "applied")

    entries = store.history(object_key("sandbox", "web", UID), NOW - 100)

    assert [e.created_at for e in entries] == [NOW - 100, NOW - 50, NOW - 10]
    applied = [e for e in entries if e.applied]
    assert [(e.plan_id, e.applied_at, e.result) for e in applied] == [
        (plans[0].plan_id, NOW - 40, "applied")
    ]
    assert all(isinstance(e.created_at, int) for e in entries)


def test_history_follows_pages(store, monkeypatch):
    for age in range(5):
        store.create(make_plan(created_at=NOW - age))
    table = store._table
    real_query = table.query
    calls = []

    def paged(**kwargs):
        calls.append(kwargs)
        return real_query(Limit=2, **kwargs)

    monkeypatch.setattr(table, "query", paged)
    assert len(store.history(object_key("sandbox", "web", UID), NOW - 10)) == 5
    assert len(calls) >= 3


def test_history_errors_propagate(store, monkeypatch):
    def broken(**_kwargs):
        raise RuntimeError("throttled")

    monkeypatch.setattr(store._table, "query", broken)
    with pytest.raises(RuntimeError):
        store.history("k", NOW)


def test_consumed_plan(store):
    plan = make_plan()
    store.create(plan)
    assert store.consumed_plan(plan.plan_id) is None
    store.consume(plan.plan_id, plan.args_hash, SCALE_DEPLOYMENT, NOW)
    # Consumed with no result recorded yet (or ever): still returned, as the annotation that
    # named it proves the patch ran.
    consumed = store.consumed_plan(plan.plan_id)
    assert consumed.replicas_to == 3 and consumed.result is None and consumed.applied_at is None
    store.record_result(plan.plan_id, NOW + 1, "applied")
    consumed = store.consumed_plan(plan.plan_id)
    assert (consumed.result, consumed.applied_at) == ("applied", NOW + 1)
    assert store.consumed_plan(str(uuid.uuid4())) is None


def test_item_round_trips_result_and_applied_at():
    plan = make_plan()
    assert "result" not in plan.to_item() and "applied_at" not in plan.to_item()
    item = plan.to_item() | {"result": "applied", "applied_at": NOW + 1}
    restored = Plan.from_item(item)
    assert (restored.result, restored.applied_at) == ("applied", NOW + 1)
    assert restored.to_item() == item


# --- Global budget ----------------------------------------------------------


def test_global_budget_counts_applies_per_clock_hour(store):
    hour = NOW - NOW % 3600
    assert store.global_applies(NOW) == 0
    store.take_global_budget(NOW, 2)
    store.take_global_budget(NOW, 2)
    assert store.global_applies(NOW) == 2
    with pytest.raises(GuardError) as exc:
        store.take_global_budget(NOW, 2)
    assert exc.value.code == "global_budget_exceeded"
    assert exc.value.details == {"limit": 2, "window_start": hour, "retry_after": hour + 3600}
    assert store.global_applies(NOW) == 2
    # The next clock hour starts from zero.
    store.take_global_budget(hour + 3600, 2)
    assert store.global_applies(hour + 3600) == 1


def test_global_budget_counter_is_not_a_plan(store):
    store.take_global_budget(NOW, 5)
    item = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table(TABLE)
        .get_item(Key={"plan_id": global_budget_key(NOW)})["Item"]
    )
    assert "object_key" not in item
    assert item["retain_until"] == NOW - NOW % 3600 + 7200
    with pytest.raises(GuardError):
        require_plan_id(global_budget_key(NOW))
