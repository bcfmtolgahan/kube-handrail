"""Per-agent identity: resolving the verified caller and who may apply a plan."""

import dataclasses

import pytest

from handrail_common.agents import check_plan_agent, guard_apply, requester, resolve_caller
from handrail_common.config import Agent, Settings
from handrail_common.observability import bind_caller, unbind_caller
from handrail_common.plans import SCALE_DEPLOYMENT, Plan
from handrail_common.validation import GuardError

SMOKE = Agent(name="smoke_test", client_id="client-a")
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
IDENTITY = dataclasses.replace(LEGACY, identity_required=True, agents={"client-a": SMOKE})


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


def test_a_plan_must_name_its_agent_in_identity_mode():
    check_plan_agent(IDENTITY, plan("smoke_test"))
    check_plan_agent(LEGACY, plan(None))
    assert forbidden(check_plan_agent, IDENTITY, plan(None)) == {"reason": "agent_unknown"}
