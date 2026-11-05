"""Server-side approval in the plan store: the lifecycle, the consume condition, the
approval request limits and what an approver is shown."""

import uuid

import pytest
from moto import mock_aws

from handrail_common.approval import (
    REASON_MAX_LENGTH,
    derived_status,
    notification,
    plan_view,
    sanitize_reason,
)
from handrail_common.plans import (
    APPROVAL_REQUEST_WINDOW_SECONDS,
    REQUESTER_GATEWAY,
    RESTART_DEPLOYMENT,
    ROLLBACK_DEPLOYMENT,
    SCALE_DEPLOYMENT,
    STATUS_AWAITING,
    STATUS_PLANNED,
    Plan,
    PlanStore,
    approval_request_key,
    plan_args_hash,
    restart_args,
    rollback_args,
    scale_args_hash,
)
from handrail_common.validation import GuardError, require_plan_id

NOW = 1_700_000_000
TABLE = "plans-test"
REGION = "us-east-1"
UID = "8f7c1a2e-0000-4000-8000-000000000001"
HUMAN = "arn:aws:iam::123456789012:user/alice"
WAIT, APPLY_TTL, COOLDOWN, LIMIT = 900, 120, 60, 10
IMPACT = {
    "pods": {"ready_now": 1, "from": 1, "to": 3, "added": 2, "removed": 0},
    "pod_disruption_budgets": [
        {
            "name": "web",
            "min_available": 1,
            "max_unavailable": None,
            "current_healthy": 1,
            "desired_healthy": 1,
            "disruptions_allowed": 0,
        }
    ],
    "services": [{"name": "web", "ready_endpoints_now": 1, "projected_ready_endpoints": 3}],
    "warnings": [{"code": "pdb_headroom", "message": "a server-generated warning"}],
    "truncated": False,
}


@pytest.fixture
def store(monkeypatch, plans_table):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        plans_table(TABLE, REGION)
        yield PlanStore.from_name(TABLE, REGION)


def make_plan(store: PlanStore, replicas: int = 3, override: bool = False, ttl: int = 300) -> Plan:
    plan = Plan(
        plan_id=str(uuid.uuid4()),
        action=SCALE_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=1,
        replicas_to=replicas,
        args_hash=scale_args_hash("sandbox", "web", replicas, override),
        resource_version="1",
        generation=4,
        uid=UID,
        created_at=NOW,
        expires_at=NOW + ttl,
        override_manual_change=override,
        impact=IMPACT,
    )
    store.create(plan)
    return plan


def code_of(call, *args, **kwargs) -> tuple[str, dict | None]:
    with pytest.raises(GuardError) as exc:
        call(*args, **kwargs)
    return exc.value.code, exc.value.details


def consume(store: PlanStore, plan: Plan, now: int = NOW, h: str | None = None) -> Plan:
    return store.consume(
        plan.plan_id, h or plan.args_hash, SCALE_DEPLOYMENT, now, require_approval=True
    )


def item(store: PlanStore, plan_id: str) -> dict:
    return store.get(plan_id)


# --- Record ------------------------------------------------------------------


def test_new_plan_is_planned_by_the_gateway_with_its_impact(store):
    plan = make_plan(store)
    record = item(store, plan.plan_id)
    assert record["status"] == STATUS_PLANNED
    assert record["requested_by"] == REQUESTER_GATEWAY == "mcp-gateway"
    assert record["override_manual_change"] is False
    assert record["impact"]["services"][0]["projected_ready_endpoints"] == 3
    assert "approved_by" not in record
    assert Plan.from_item(record).to_item() == record


# --- Consume in server mode ----------------------------------------------------


def test_server_mode_refuses_an_unapproved_plan_and_leaves_it_usable(store):
    plan = make_plan(store)
    assert code_of(consume, store, plan) == ("approval_required", {"status": "planned"})
    store.request_approval(plan.plan_id, NOW, WAIT, COOLDOWN, LIMIT)
    assert code_of(consume, store, plan) == ("approval_required", {"status": "awaiting_approval"})
    assert item(store, plan.plan_id)["used"] is False
    # The same plan is still usable in client mode: the condition there is unchanged.
    assert store.consume(plan.plan_id, plan.args_hash, SCALE_DEPLOYMENT, NOW).used


def test_server_mode_consumes_an_approved_plan_once(store):
    plan = make_plan(store)
    store.approve(plan.plan_id, HUMAN, NOW, APPLY_TTL)
    consumed = consume(store, plan, NOW + 10)
    assert consumed.used and consumed.approved_by == HUMAN
    assert consumed.approved_args_hash == plan.args_hash
    assert code_of(consume, store, plan, NOW + 11)[0] == "plan_used"


def test_approval_is_pinned_to_the_arguments(store):
    plan = make_plan(store)
    store.approve(plan.plan_id, HUMAN, NOW, APPLY_TTL)
    other = scale_args_hash("sandbox", "web", 2)
    assert code_of(consume, store, plan, NOW, other)[0] == "plan_mismatch"
    overridden = scale_args_hash("sandbox", "web", 3, True)
    assert code_of(consume, store, plan, NOW, overridden)[0] == "plan_mismatch"
    assert consume(store, plan).used


def test_approval_by_the_requester_is_refused(store):
    plan = make_plan(store)
    code, _ = code_of(store.approve, plan.plan_id, REQUESTER_GATEWAY, NOW, APPLY_TTL)
    assert code == "forbidden"
    assert item(store, plan.plan_id)["status"] == STATUS_PLANNED


def test_an_approval_naming_the_requester_never_consumes(store):
    # Written behind the approval channel's back: the consume condition still refuses it.
    plan = make_plan(store)
    store.approve(plan.plan_id, HUMAN, NOW, APPLY_TTL)
    store._table.update_item(
        Key={"plan_id": plan.plan_id},
        UpdateExpression="SET approved_by = requested_by",
    )
    assert code_of(consume, store, plan) == ("approval_required", {"status": "approval_invalid"})


def test_an_approval_of_other_arguments_never_consumes(store):
    plan = make_plan(store)
    store.approve(plan.plan_id, HUMAN, NOW, APPLY_TTL)
    store._table.update_item(
        Key={"plan_id": plan.plan_id},
        UpdateExpression="SET approved_args_hash = :h",
        ExpressionAttributeValues={":h": scale_args_hash("sandbox", "web", 2)},
    )
    assert code_of(consume, store, plan) == ("approval_required", {"status": "approval_invalid"})


def test_an_expired_approval_is_reported(store):
    plan = make_plan(store)
    store.approve(plan.plan_id, HUMAN, NOW, APPLY_TTL)
    late = NOW + APPLY_TTL
    assert code_of(consume, store, plan, late) == (
        "approval_required",
        {"status": "approval_expired"},
    )
    # Client mode sees the same record as an expired plan.
    code, _ = code_of(store.consume, plan.plan_id, plan.args_hash, SCALE_DEPLOYMENT, late)
    assert code == "plan_expired"


def test_a_rejected_plan_cannot_be_applied_in_any_mode(store):
    plan = make_plan(store)
    store.reject(plan.plan_id, HUMAN, NOW, "too big")
    assert code_of(consume, store, plan) == ("approval_required", {"status": "rejected"})
    code, details = code_of(store.consume, plan.plan_id, plan.args_hash, SCALE_DEPLOYMENT, NOW)
    assert (code, details) == ("approval_required", {"status": "rejected"})


def test_a_rejected_plan_is_not_consumed_even_with_clock_skew(store):
    # Rejected on a clock ahead of the applier's: expires_at is still in the applier's future.
    plan = make_plan(store)
    store.reject(plan.plan_id, HUMAN, NOW + 30, "")
    for require_approval in (False, True):
        with pytest.raises(GuardError) as exc:
            store.consume(
                plan.plan_id,
                plan.args_hash,
                SCALE_DEPLOYMENT,
                NOW,
                require_approval=require_approval,
            )
        assert (exc.value.code, exc.value.details) == ("approval_required", {"status": "rejected"})
    assert item(store, plan.plan_id)["used"] is False


def test_unknown_plan_in_server_mode(store):
    plan = make_plan(store)
    missing = Plan(**{**plan.__dict__, "plan_id": str(uuid.uuid4())})
    assert code_of(consume, store, missing)[0] == "plan_not_found"


# --- Decisions -----------------------------------------------------------------


def test_approve_records_the_approval_and_the_split_ttl(store):
    plan = make_plan(store)
    store.request_approval(plan.plan_id, NOW + 5, WAIT, COOLDOWN, LIMIT)
    record = store.approve(plan.plan_id, HUMAN, NOW + 100, APPLY_TTL)
    assert record["status"] == "approved"
    assert record["approved_by"] == HUMAN
    assert record["approved_at"] == NOW + 100
    assert record["approved_args_hash"] == plan.args_hash
    assert record["approval_channel"] == "api"
    # Apply must follow promptly, whatever was left of the wait.
    assert record["approval_expires_at"] == record["expires_at"] == NOW + 100 + APPLY_TTL


def test_a_plan_can_be_approved_without_a_request(store):
    plan = make_plan(store)
    assert store.approve(plan.plan_id, HUMAN, NOW, APPLY_TTL)["status"] == "approved"


@pytest.mark.parametrize("second", ["approve", "reject"])
def test_a_decision_is_final(store, second):
    plan = make_plan(store)
    store.approve(plan.plan_id, HUMAN, NOW, APPLY_TTL)
    decide = store.approve if second == "approve" else store.reject
    args = (APPLY_TTL,) if second == "approve" else ("",)
    assert code_of(decide, plan.plan_id, HUMAN, NOW + 1, *args) == (
        "plan_decided",
        {"status": "approved"},
    )


def test_used_expired_and_unknown_plans_cannot_be_decided(store):
    used = make_plan(store)
    store.consume(used.plan_id, used.args_hash, SCALE_DEPLOYMENT, NOW)
    assert code_of(store.approve, used.plan_id, HUMAN, NOW, APPLY_TTL)[0] == "plan_used"
    expired = make_plan(store)
    assert code_of(store.approve, expired.plan_id, HUMAN, NOW + 300, APPLY_TTL)[0] == "plan_expired"
    assert code_of(store.reject, expired.plan_id, HUMAN, NOW + 300, "")[0] == "plan_expired"
    unknown = str(uuid.uuid4())
    assert code_of(store.approve, unknown, HUMAN, NOW, APPLY_TTL)[0] == "plan_not_found"


def test_plans_made_before_server_approval_cannot_be_decided(store):
    plan = make_plan(store)
    store._table.update_item(
        Key={"plan_id": plan.plan_id},
        UpdateExpression="REMOVE #s, requested_by",
        ExpressionAttributeNames={"#s": "status"},
    )
    assert code_of(store.approve, plan.plan_id, HUMAN, NOW, APPLY_TTL)[0] == "plan_expired"
    assert (
        code_of(store.request_approval, plan.plan_id, NOW, WAIT, COOLDOWN, LIMIT)[0]
        == "plan_expired"
    )


def test_reject_records_the_reason_and_ends_the_plan(store):
    plan = make_plan(store)
    record = store.reject(plan.plan_id, HUMAN, NOW + 7, "not now")
    assert record["status"] == "rejected"
    assert (record["rejected_by"], record["rejected_at"]) == (HUMAN, NOW + 7)
    assert record["reject_reason"] == "not now"
    assert record["expires_at"] == NOW + 7
    assert code_of(store.request_approval, plan.plan_id, NOW + 8, WAIT, COOLDOWN, LIMIT) == (
        "plan_decided",
        {"status": "rejected"},
    )


# --- request_approval ----------------------------------------------------------


def test_request_approval_waits_longer_and_is_idempotent(store):
    plan = make_plan(store)
    record, retry_after = store.request_approval(plan.plan_id, NOW + 10, WAIT, COOLDOWN, LIMIT)
    assert retry_after is None
    assert record["status"] == STATUS_AWAITING
    assert record["approval_requested_at"] == NOW + 10
    # From creation, not from the request, so asking again never extends it.
    assert record["expires_at"] == NOW + WAIT

    record, retry_after = store.request_approval(plan.plan_id, NOW + 20, WAIT, COOLDOWN, LIMIT)
    assert retry_after == NOW + 10 + COOLDOWN
    assert record["approval_requested_at"] == NOW + 10

    record, retry_after = store.request_approval(
        plan.plan_id, NOW + 10 + COOLDOWN, WAIT, COOLDOWN, LIMIT
    )
    assert retry_after is None and record["approval_requested_at"] == NOW + 10 + COOLDOWN
    assert record["expires_at"] == NOW + WAIT


def test_request_approval_within_the_cooldown_takes_no_global_unit(store):
    plan = make_plan(store)
    store.request_approval(plan.plan_id, NOW, WAIT, COOLDOWN, LIMIT)
    for second in range(1, 5):
        store.request_approval(plan.plan_id, NOW + second, WAIT, COOLDOWN, LIMIT)
    counter = store.get(approval_request_key(NOW))
    assert counter["requests"] == 1


def test_request_approval_refuses_finished_plans(store):
    used = make_plan(store)
    store.consume(used.plan_id, used.args_hash, SCALE_DEPLOYMENT, NOW)
    assert (
        code_of(store.request_approval, used.plan_id, NOW, WAIT, COOLDOWN, LIMIT)[0] == "plan_used"
    )
    expired = make_plan(store)
    code, _ = code_of(store.request_approval, expired.plan_id, NOW + 300, WAIT, COOLDOWN, LIMIT)
    assert code == "plan_expired"
    approved = make_plan(store)
    store.approve(approved.plan_id, HUMAN, NOW, APPLY_TTL)
    assert code_of(store.request_approval, approved.plan_id, NOW, WAIT, COOLDOWN, LIMIT) == (
        "plan_decided",
        {"status": "approved"},
    )
    unknown = str(uuid.uuid4())
    code, _ = code_of(store.request_approval, unknown, NOW, WAIT, COOLDOWN, LIMIT)
    assert code == "plan_not_found"


def test_request_approval_global_limit(store):
    window = NOW - NOW % APPROVAL_REQUEST_WINDOW_SECONDS
    plans = [make_plan(store) for _ in range(3)]
    store.request_approval(plans[0].plan_id, NOW, WAIT, COOLDOWN, 2)
    store.request_approval(plans[1].plan_id, NOW, WAIT, COOLDOWN, 2)
    code, details = code_of(store.request_approval, plans[2].plan_id, NOW, WAIT, COOLDOWN, 2)
    assert code == "approval_rate_limited"
    assert details == {
        "limit": 2,
        "window_seconds": 600,
        "window_start": window,
        "retry_after": window + 600,
    }
    # The refused plan did not change state.
    assert item(store, plans[2].plan_id)["status"] == STATUS_PLANNED
    # A new window starts from zero.
    later = window + APPROVAL_REQUEST_WINDOW_SECONDS
    plan = make_plan(store, ttl=WAIT + 600)
    assert store.request_approval(plan.plan_id, later, WAIT, COOLDOWN, 2)[1] is None


def test_request_approval_counter_is_atomic(store, monkeypatch):
    """The limit is the update's own condition, not a read before it: a stale read cannot
    let a request through once the counter is at the limit."""
    plans = [make_plan(store) for _ in range(2)]
    store.request_approval(plans[0].plan_id, NOW, WAIT, COOLDOWN, 1)
    calls = []
    real = store._table.update_item

    def recording(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(store._table, "update_item", recording)
    assert code_of(store.request_approval, plans[1].plan_id, NOW, WAIT, COOLDOWN, 1)[0] == (
        "approval_rate_limited"
    )
    (counter,) = calls
    assert counter["ConditionExpression"] == "attribute_not_exists(#count) OR #count < :limit"
    assert counter["UpdateExpression"].startswith("ADD #count :one")


def test_approval_counter_is_not_a_plan(store):
    plan = make_plan(store)
    store.request_approval(plan.plan_id, NOW, WAIT, COOLDOWN, LIMIT)
    counter = store.get(approval_request_key(NOW))
    window = NOW - NOW % APPROVAL_REQUEST_WINDOW_SECONDS
    assert "object_key" not in counter
    assert counter["retain_until"] == window + 2 * APPROVAL_REQUEST_WINDOW_SECONDS
    with pytest.raises(GuardError):
        require_plan_id(approval_request_key(NOW))


# --- What the approver sees ----------------------------------------------------


def test_plan_view_and_derived_status(store):
    plan = make_plan(store, override=True)
    view = plan_view(item(store, plan.plan_id), NOW)
    assert view["status"] == "planned"
    assert view["replicas"] == {"from": 1, "to": 3}
    assert view["override_manual_change"] is True
    assert view["impact"] == IMPACT
    assert view["change"] == {"replicas": {"from": 1, "to": 3}}
    assert isinstance(view["expires_at"], int)
    assert "approval" not in view and "rejection" not in view
    assert derived_status(item(store, plan.plan_id), NOW + 300) == "expired"

    store.approve(plan.plan_id, HUMAN, NOW, APPLY_TTL)
    view = plan_view(item(store, plan.plan_id), NOW)
    assert view["status"] == "approved"
    assert view["approval"]["approved_by"] == HUMAN
    store.consume(plan.plan_id, plan.args_hash, SCALE_DEPLOYMENT, NOW, require_approval=True)
    assert plan_view(item(store, plan.plan_id), NOW + 1000)["status"] == "applied"


def test_notification_is_rendered_from_the_record(store):
    plan = make_plan(store, override=True)
    message = notification(plan_view(item(store, plan.plan_id), NOW), "https://approve.example")
    assert f"Plan: {plan.plan_id}" in message
    assert "Deployment: sandbox/web" in message
    assert "Replicas: 1 -> 3" in message
    assert "Override manual change: yes" in message
    assert "- pdb_headroom: a server-generated warning" in message
    assert "Service web: 1 ready endpoints now, 3 projected" in message
    assert f"Expires at (epoch seconds): {NOW + 300}" in message
    assert (
        f"APPROVAL_URL=https://approve.example scripts/approve.py approve {plan.plan_id}" in message
    )
    assert f"scripts/approve.py reject {plan.plan_id}" in message


ROLLOUT_IMPACT = {
    "pods": {"ready_now": 2, "from": 2, "to": 2, "added": 0, "removed": 0, "replaced": 2},
    "rollout": {"strategy": "RollingUpdate", "max_unavailable": 0, "max_surge": 1},
    "pod_disruption_budgets": [],
    "services": [{"name": "web", "ready_endpoints_now": 2, "projected_min_ready_endpoints": 2}],
    "warnings": [],
    "truncated": False,
}


def make_restart_plan(store: PlanStore, impact: dict = ROLLOUT_IMPACT) -> Plan:
    plan = Plan(
        plan_id=str(uuid.uuid4()),
        action=RESTART_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=2,
        replicas_to=2,
        args_hash=plan_args_hash(RESTART_DEPLOYMENT, restart_args("sandbox", "web")),
        args=restart_args("sandbox", "web"),
        resource_version="1",
        generation=4,
        uid=UID,
        created_at=NOW,
        expires_at=NOW + 300,
        impact=impact,
        template_from="sha256:a",
        template_to="sha256:b",
    )
    store.create(plan)
    return plan


def test_restart_view_shows_the_pods_replaced_and_the_templates(store):
    plan = make_restart_plan(store)
    view = plan_view(item(store, plan.plan_id), NOW)
    assert view["action"] == "restart_deployment"
    assert view["change"] == {
        "restart": {"pods_replaced": 2},
        "template": {"from": "sha256:a", "to": "sha256:b"},
    }


def test_restart_notification_describes_the_rollout(store):
    plan = make_restart_plan(store)
    message = notification(plan_view(item(store, plan.plan_id), NOW), None)
    assert (
        "Restart: every pod replaced, replicas stay at 2; pods get the annotation "
        f"kube-handrail.io/restart-plan-id={plan.plan_id}"
    ) in message
    assert "Replicas:" not in message
    assert "Pods: 2 ready now, 2 replaced" in message
    assert "Rollout: RollingUpdate, up to 0 unavailable, 1 surge" in message
    assert "Service web: 2 ready endpoints now, 2 projected at the lowest" in message


def test_recreate_notification_says_every_pod_stops_first(store):
    impact = {**ROLLOUT_IMPACT, "rollout": {"strategy": "Recreate", "max_unavailable": None}}
    plan = make_restart_plan(store, impact)
    message = notification(plan_view(item(store, plan.plan_id), NOW), None)
    assert "Rollout: Recreate, every pod stops first" in message


def make_rollback_plan(store: PlanStore, **fields) -> Plan:
    plan = Plan(
        plan_id=str(uuid.uuid4()),
        action=ROLLBACK_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=2,
        replicas_to=2,
        args_hash=plan_args_hash(ROLLBACK_DEPLOYMENT, rollback_args("sandbox", "web", 3)),
        args=rollback_args("sandbox", "web", 3),
        resource_version="1",
        generation=4,
        uid=UID,
        created_at=NOW,
        expires_at=NOW + 300,
        impact={
            **ROLLOUT_IMPACT,
            "warnings": [{"code": "mutable_image_tag", "message": "not pinned by digest"}],
        },
        template_from="sha256:a",
        template_to="sha256:b",
        revision_from=5,
        revision_to=3,
        target_rs_name="web-7d5b9c6f8",
        target_rs_uid="uid-3",
        images=[
            {
                "container": "nginx",
                "init": False,
                "from": "<untrusted>nginx:1.27.5</untrusted>",
                "to": "<untrusted>nginx:1.27.4</untrusted>",
                "changed": True,
                "pinned_by_digest": False,
            },
            {
                "container": "setup",
                "init": True,
                "from": "<untrusted>busybox:1</untrusted>",
                "to": "<untrusted>busybox:1</untrusted>",
                "changed": False,
                "pinned_by_digest": False,
            },
        ],
        fields_changed=["spec.containers[nginx].env", "spec.containers[nginx].image"],
        **fields,
    )
    store.create(plan)
    return plan


def test_rollback_view_shows_the_revision_replica_set_images_and_fields(store):
    plan = make_rollback_plan(store)
    view = plan_view(item(store, plan.plan_id), NOW)
    assert view["action"] == "rollback_deployment"
    assert view["change"] == {
        "revision": {"from": 5, "to": 3},
        "template": {"from": "sha256:a", "to": "sha256:b"},
        "replica_set": "web-7d5b9c6f8",
        "images": plan.images,
        "fields_changed": ["spec.containers[nginx].env", "spec.containers[nginx].image"],
        "truncated": False,
    }


def test_rollback_notification_lists_image_changes_and_other_fields(store):
    plan = make_rollback_plan(store)
    message = notification(plan_view(item(store, plan.plan_id), NOW), None)
    assert (
        "Rollback: revision 5 -> 3 (ReplicaSet web-7d5b9c6f8); replicas stay as they are"
    ) in message
    assert (
        "Image of container nginx: <untrusted>nginx:1.27.5</untrusted> -> "
        "<untrusted>nginx:1.27.4</untrusted>"
    ) in message
    assert "setup" not in message
    assert "Other changed fields: spec.containers[nginx].env" in message
    assert "- mutable_image_tag: not pinned by digest" in message
    assert "Replicas:" not in message
    assert "truncated" not in message


def test_rollback_notification_says_when_the_change_lists_were_cut(store):
    plan = make_rollback_plan(store, diff_truncated=True)
    view = plan_view(item(store, plan.plan_id), NOW)
    assert view["change"]["truncated"] is True
    assert "(image or field list truncated)" in notification(view, None)


def test_sanitize_reason():
    assert sanitize_reason(None) == ""
    assert sanitize_reason("  ok\tfine\nreally\x00\x1b[31m ") == "ok fine really[31m"
    assert sanitize_reason("a" * REASON_MAX_LENGTH) == "a" * REASON_MAX_LENGTH
    for bad in ("a" * (REASON_MAX_LENGTH + 1), 5, ["x"]):
        with pytest.raises(GuardError):
            sanitize_reason(bad)
