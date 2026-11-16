"""Kill switches (global and per agent), change freezes, the hourly budgets (global, per
agent, per object) and the agent identity behind them, including forged callers
(tests/e2e/e2e_gateway.py: plan_tests, identity_tests, agent_switch_tests,
agent_budget_tests, freeze_cases). The budgets are reached with real applies against small
limits, so no case waits for the clock hour."""

import time

from harness import (
    AGENT_A,
    AGENT_B,
    AGENT_SWITCHES,
    CALLER_KEY,
    CLIENT_A,
    CLIENT_B,
    KILL_SWITCH,
    NAMESPACE,
    code,
    deployment_manifest,
    details,
    unique,
    wait_out_hour_boundary,
)

from handrail_common.dispatch import CALLER_ARGUMENT, caller_signature
from handrail_common.freezes import Freeze, format_time

PLAN = "k8sr___plan_scale_deployment"
STATUS = "k8sr___get_workload_status"


def fixture(kh, prefix, replicas=1):
    return kh.create(deployment_manifest(unique(prefix), replicas))


def test_global_kill_switch_takes_effect_immediately(kh):
    target = fixture(kh, "it-kill")
    plan_id = kh.plan(target, 2)
    kh.set_param(KILL_SWITCH, "false")
    envelope = kh.apply(target, plan_id, 2)
    assert code(envelope) == "remediation_disabled"
    assert kh.deployment(target["name"]).spec.replicas == 1
    # Any value but the exact "true" is off, and so is a parameter that cannot be read.
    kh.set_param(KILL_SWITCH, "TRUE")
    assert code(kh.apply(target, kh.plan(target, 2), 2)) == "remediation_disabled"
    kh.ssm.delete_parameter(Name=KILL_SWITCH)
    assert code(kh.apply(target, kh.plan(target, 2), 2)) == "remediation_disabled"
    kh.set_param(KILL_SWITCH, "true")
    assert kh.apply(target, kh.plan(target, 2), 2)["ok"] is True


def test_agent_kill_switch_stops_only_that_agent(kh):
    target = fixture(kh, "it-agent-switch")
    plan_id = kh.plan(target, 2, client=CLIENT_B)
    kh.set_param(f"{AGENT_SWITCHES}{AGENT_B}/enabled", "false")
    disabled = {"scope": "agent", "agent": AGENT_B}
    envelope = kh.apply(target, plan_id, 2, client=CLIENT_B)
    assert code(envelope) == "remediation_disabled" and details(envelope) == disabled
    assert kh.record(plan_id)["used"] is True
    envelope = kh.call(PLAN, {**target, "replicas": 2}, client=CLIENT_B)
    assert code(envelope) == "remediation_disabled" and details(envelope) == disabled
    assert kh.call(PLAN, {**target, "replicas": 2}, client=CLIENT_A)["ok"] is True


def test_plan_names_the_verified_agent_and_ignores_a_client_sent_caller(kh):
    target = fixture(kh, "it-identity")
    planned = kh.call(PLAN, {**target, "replicas": 2})
    record = kh.record(planned["data"]["plan_id"])
    assert record["requested_by"] == f"agent:{AGENT_A}" == planned["data"]["requested_by"]
    assert record["agent"] == AGENT_A and record["requested_client_id"] == CLIENT_A
    # The interceptor overwrites whatever the client sent under the reserved name.
    spoofed = kh.call(PLAN, {**target, "replicas": 2, CALLER_ARGUMENT: CLIENT_B})
    assert kh.record(spoofed["data"]["plan_id"])["agent"] == AGENT_A


def test_another_agent_cannot_apply_the_plan(kh):
    target = fixture(kh, "it-not-requester")
    plan_id = kh.plan(target, 2)
    envelope = kh.apply(target, plan_id, 2, client=CLIENT_B)
    assert code(envelope) == "forbidden" and details(envelope) == {"reason": "not_requester"}
    assert kh.record(plan_id)["used"] is False


def test_forged_callers_are_refused(kh):
    """Without the interceptor (a gateway that lost it), a caller the client set itself is
    never trusted: unsigned, signed with another key, or a signed but unknown client."""
    target = fixture(kh, "it-forged")
    arguments = {**target, "replicas": 2}
    unknown = {"reason": "agent_unknown"}
    forged = [
        {},
        {CALLER_ARGUMENT: CLIENT_A},
        {CALLER_ARGUMENT: f"{CLIENT_A}.{caller_signature('another-key', CLIENT_A)}"},
        {CALLER_ARGUMENT: f"{CLIENT_A}.{caller_signature(CALLER_KEY, CLIENT_B)}"},
        {CALLER_ARGUMENT: f"kh-int-unknown.{caller_signature(CALLER_KEY, 'kh-int-unknown')}"},
    ]
    for extra in forged:
        envelope = kh.call(PLAN, {**arguments, **extra}, interceptor=False)
        assert code(envelope) == "forbidden" and details(envelope) == unknown, extra
    # The same unknown client through the interceptor.
    envelope = kh.call(PLAN, arguments, client="kh-int-unknown")
    assert code(envelope) == "forbidden" and details(envelope) == unknown
    # A correctly signed caller is what the interceptor produces.
    signed = {CALLER_ARGUMENT: f"{CLIENT_A}.{caller_signature(CALLER_KEY, CLIENT_A)}"}
    assert kh.call(PLAN, {**arguments, **signed}, interceptor=False)["ok"] is True
    # Reads need no agent.
    assert kh.call(STATUS, target, interceptor=False)["ok"] is True


def test_global_budget(kh):
    wait_out_hour_boundary()
    kh.configure(GLOBAL_HOURLY_APPLY_BUDGET="2")
    first, second, third = (fixture(kh, f"it-global-{i}") for i in range(3))
    late = kh.plan(third, 2)
    for target in (first, second):
        assert kh.apply(target, kh.plan(target, 2), 2)["ok"] is True
    envelope = kh.call(PLAN, {**third, "replicas": 2})
    assert code(envelope) == "global_budget_exceeded" and details(envelope)["retry_after"]
    # A plan made before the budget was spent is refused at apply.
    assert code(kh.apply(third, late, 2)) == "global_budget_exceeded"
    assert kh.deployment(third["name"]).spec.replicas == 1


def test_agent_budget(kh):
    wait_out_hour_boundary()
    targets = [fixture(kh, f"it-agent-budget-{i}") for i in range(3)]
    late = kh.plan(targets[2], 2, client=CLIENT_B)
    for target in targets[:2]:
        assert kh.apply(target, kh.plan(target, 2, client=CLIENT_B), 2, client=CLIENT_B)["ok"]
    envelope = kh.call(PLAN, {**targets[2], "replicas": 2}, client=CLIENT_B)
    assert code(envelope) == "agent_budget_exceeded" and details(envelope)["limit"] == 2
    assert code(kh.apply(targets[2], late, 2, client=CLIENT_B)) == "agent_budget_exceeded"
    # Another agent still applies.
    assert kh.apply(targets[2], kh.plan(targets[2], 2), 2)["ok"] is True


def test_object_budget(kh):
    wait_out_hour_boundary()
    kh.configure(OBJECT_HOURLY_APPLY_BUDGET="2")
    target = fixture(kh, "it-object-budget")
    for replicas in (2, 3):
        assert kh.apply(target, kh.plan(target, replicas), replicas)["ok"] is True
    envelope = kh.call(PLAN, {**target, "replicas": 4})
    assert code(envelope) == "object_budget_exceeded"
    assert details(envelope)["limit"] == 2 and details(envelope)["count"] == 2


def freeze(namespace, now, reason="it freeze"):
    return Freeze(namespaces=(namespace,), start=now - 60, until=now + 1800, reason=reason)


def test_freeze(kh):
    target = fixture(kh, "it-freeze")
    now = int(time.time())
    kh.set_freezes(freeze("it-freeze-other", now))
    assert kh.call(PLAN, {**target, "replicas": 2})["ok"] is True

    planned_before = kh.plan(target, 2)
    kh.set_freezes(freeze(NAMESPACE, now))
    expected = {"namespace": NAMESPACE, "reason": "it freeze", "until": format_time(now + 1800)}
    envelope = kh.call(PLAN, {**target, "replicas": 2})
    assert code(envelope) == "change_frozen"
    assert {k: details(envelope).get(k) for k in expected} == expected
    envelope = kh.apply(target, planned_before, 2)
    assert code(envelope) == "change_frozen"
    assert kh.record(planned_before)["used"] is False

    kh.set_freezes()
    assert kh.apply(target, planned_before, 2)["ok"] is True
    # A freeze parameter that does not validate refuses every write.
    kh.set_param("/kh-int/freezes", "not json")
    envelope = kh.call(PLAN, {**target, "replicas": 3})
    assert code(envelope) == "remediation_disabled" and details(envelope)["scope"] == "freezes"
