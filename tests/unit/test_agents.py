"""Per-agent identity: resolving the verified caller and who may apply a plan."""

import dataclasses

import pytest

from handrail_common.agents import (
    check_agent_enabled,
    check_plan_agent,
    guard_apply,
    guard_plan,
    requester,
    resolve_caller,
)
from handrail_common.config import Agent, Settings
from handrail_common.observability import bind_caller, unbind_caller
from handrail_common.plans import SCALE_DEPLOYMENT, Plan
from handrail_common.validation import GuardError

SMOKE = Agent(name="smoke_test", client_id="client-a", hourly_apply_budget=3)
LEGACY = Settings(
    cluster_name="c",
    plans_table="t",
    allowed_namespaces=frozenset({"sandbox"}),
    min_replicas=0,
    max_replicas=5,
    plan_ttl_seconds=300,
    remediation_enabled_param=None,
    region=None,
)
IDENTITY = dataclasses.replace(
    LEGACY,
    identity_required=True,
    agents={"client-a": SMOKE},
    agent_kill_switch_prefix="/kh/agents/",
)


@pytest.fixture
def caller():
    """Bind a caller and channel as dispatch does, for one test."""
    bound = []

    def bind(value: str | None, channel: str = "gateway") -> None:
        bound.append(bind_caller(value, channel))

    yield bind
    for tokens in reversed(bound):
        unbind_caller(tokens)


def forbidden(call, *args) -> dict | None:
    with pytest.raises(GuardError) as exc:
        call(*args)
    assert exc.value.code == "forbidden"
    return exc.value.details


def plan(agent: str | None) -> Plan:
    return Plan(
        plan_id="p",
        action=SCALE_DEPLOYMENT,
        namespace="sandbox",
        name="web",
        replicas_from=1,
        replicas_to=2,
        args_hash="h",
        resource_version="1",
        generation=1,
        uid="u",
        created_at=0,
        expires_at=300,
        requested_by=f"agent:{agent}" if agent else "mcp-gateway",
        agent=agent,
    )


def test_requester():
    assert requester(SMOKE) == "agent:smoke_test"
    assert requester(None) == "mcp-gateway"


def test_legacy_mode_names_nobody_whatever_the_caller(caller):
    caller("client-a")
    assert resolve_caller(LEGACY) is None
    assert guard_apply(LEGACY) is None


def test_identity_mode_resolves_a_listed_client(caller):
    caller("client-a")
    assert resolve_caller(IDENTITY) == SMOKE
    assert guard_apply(IDENTITY) == "agent:smoke_test"


@pytest.mark.parametrize("value", [None, "unknown", "client-b", ""])
def test_identity_mode_refuses_a_caller_it_does_not_know(caller, value):
    caller(value)
    assert forbidden(resolve_caller, IDENTITY) == {"reason": "agent_unknown"}
    assert forbidden(guard_apply, IDENTITY) == {"reason": "agent_unknown"}


def test_outside_dispatch_there_is_no_caller():
    assert forbidden(resolve_caller, IDENTITY) == {"reason": "agent_unknown"}


def test_the_approval_channel_expects_no_caller(caller):
    caller(None, "approval-api")
    assert guard_apply(IDENTITY) is None


class Switches:
    """An SSM client holding the parameters given."""

    def __init__(self, values: dict[str, str] | None = None, error: bool = False) -> None:
        self.values = values or {}
        self.error = error
        self.names: list[str] = []

    def get_parameter(self, Name):
        self.names.append(Name)
        if self.error or Name not in self.values:
            raise RuntimeError("cannot read")
        return {"Parameter": {"Value": self.values[Name]}}


SWITCH = "/kh/agents/smoke_test/enabled"


def test_a_plan_must_name_its_agent_in_identity_mode():
    on = Switches({SWITCH: "true"})
    check_plan_agent(IDENTITY, on, plan("smoke_test"))
    check_plan_agent(LEGACY, Switches(error=True), plan(None))
    assert forbidden(check_plan_agent, IDENTITY, on, plan(None)) == {"reason": "agent_unknown"}


@pytest.mark.parametrize(
    "ssm",
    [
        Switches({SWITCH: "false"}),
        Switches({SWITCH: "TRUE"}),
        Switches({SWITCH: "true "}),
        Switches({}),
        Switches({SWITCH: "true"}, error=True),
    ],
)
def test_only_an_exact_true_lets_the_agent_act(ssm):
    with pytest.raises(GuardError) as exc:
        check_agent_enabled(ssm, IDENTITY, "smoke_test")
    assert exc.value.code == "remediation_disabled"
    assert exc.value.details == {"scope": "agent", "agent": "smoke_test"}


def test_the_switch_is_read_on_every_call(caller):
    ssm = Switches({SWITCH: "true"})
    caller("client-a")
    assert guard_plan(IDENTITY, ssm) == SMOKE
    ssm.values[SWITCH] = "false"
    with pytest.raises(GuardError):
        guard_plan(IDENTITY, ssm)
    assert ssm.names == [SWITCH, SWITCH]


def test_legacy_mode_reads_no_switch(caller):
    caller("client-a")
    ssm = Switches(error=True)
    assert guard_plan(LEGACY, ssm) is None
    assert ssm.names == []


def test_an_agent_removed_from_agents_gets_the_default_budget():
    assert IDENTITY.agent_budget("smoke_test") == 3
    assert IDENTITY.agent_budget("gone") == IDENTITY.agent_hourly_apply_budget == 10
