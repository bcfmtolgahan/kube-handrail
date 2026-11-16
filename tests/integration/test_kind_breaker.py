"""The oscillation breaker: cooldown, flapping and manual changes
(tests/e2e/e2e_gateway.py: breaker_tests). The write Lambda's clock is moved back for
earlier applies instead of waiting out the cooldown."""

from harness import code, deployment_manifest, details, unique

PLAN = "k8sr___plan_scale_deployment"


def test_cooldown(kh):
    kh.configure(APPLY_COOLDOWN_SECONDS="60")
    target = kh.create(deployment_manifest(unique("it-cooldown")))
    assert kh.apply(target, kh.plan(target, 2), 2)["ok"] is True
    envelope = kh.call(PLAN, {**target, "replicas": 1})
    assert code(envelope) == "cooldown"
    assert details(envelope)["retry_after"] == details(envelope)["last_applied_at"] + 60

    other = kh.create(deployment_manifest(unique("it-cooldown-past")))
    plan_id = kh.plan(other, 2)
    kh.write_offset = -61
    assert kh.apply(other, plan_id, 2)["ok"] is True
    assert kh.call(PLAN, {**other, "replicas": 1})["ok"] is True


def test_single_revert_is_allowed_a_second_reversal_is_flapping(kh):
    target = kh.create(deployment_manifest(unique("it-flap")))
    up = kh.plan(target, 2)
    kh.write_offset = -20
    assert kh.apply(target, up, 2)["ok"] is True
    down = kh.plan(target, 1)
    kh.write_offset = -10
    assert kh.apply(target, down, 1)["ok"] is True
    kh.write_offset = 0
    envelope = kh.call(PLAN, {**target, "replicas": 2})
    assert code(envelope) == "flapping"
    assert [(h["from"], h["to"]) for h in details(envelope)["history"]] == [(1, 2), (2, 1)]


def test_manual_change_needs_the_override(kh):
    target = kh.create(deployment_manifest(unique("it-manual")))
    assert kh.apply(target, kh.plan(target, 2), 2)["ok"] is True
    kh.scale_by_hand(target["name"], 3)
    envelope = kh.call(PLAN, {**target, "replicas": 1})
    assert code(envelope) == "manual_change"
    assert details(envelope) == {"last_applied_to": 2, "current": 3}
    plan_id = kh.plan(target, 1, override_manual_change=True)
    assert code(kh.apply(target, plan_id, 1)) == "plan_mismatch"
    assert kh.apply(target, plan_id, 1, override_manual_change=True)["ok"] is True
    assert kh.deployment(target["name"]).spec.replicas == 1
