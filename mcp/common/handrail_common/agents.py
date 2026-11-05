"""Per-agent identity, budgets and kill switches (mcp/CONTRACT.md, Agent identity): which
agent asked for a plan, which may request its approval or apply it, and whether it may act.

Every plan_* and apply_* tool runs through the generic plan and apply paths, which call
guard_plan and guard_apply, so a new action cannot skip them.
"""

from typing import Any

from handrail_common.config import Agent, Settings
from handrail_common.envelope import FORBIDDEN, REMEDIATION_DISABLED
from handrail_common.killswitch import KillSwitch
from handrail_common.observability import CHANNEL_GATEWAY, current_caller, current_channel
from handrail_common.plans import (
    GLOBAL_BUDGET_WINDOW_SECONDS,
    REQUESTER_GATEWAY,
    Plan,
    PlanStore,
    agent_budget_error,
)
from handrail_common.validation import GuardError

REQUESTER_PREFIX = "agent:"
# details.reason of the forbidden code when identity mode finds no configured agent.
AGENT_UNKNOWN = "agent_unknown"


def requester(agent: Agent | None) -> str:
    """The plan record's requested_by: agent:<name>, or the gateway class in legacy mode."""
    return REQUESTER_GATEWAY if agent is None else f"{REQUESTER_PREFIX}{agent.name}"


def agent_unknown_error(message: str) -> GuardError:
    # The client_id is in the tool record, never in the response.
    return GuardError(FORBIDDEN, message, {"reason": AGENT_UNKNOWN})


def resolve_caller(settings: Settings) -> Agent | None:
    """The verified caller's agent; None in legacy mode. In identity mode a caller that is
    missing, `unknown` or not in AGENTS is refused."""
    if not settings.identity_required:
        return None
    agent = settings.agents.get(current_caller() or "")
    if agent is None:
        raise agent_unknown_error("the caller is not a configured agent")
    return agent


def check_agent_enabled(ssm: Any, settings: Settings, name: str) -> None:
    """The agent's own kill switch, read on every call: only the exact value "true" lets it
    act, and a parameter that cannot be read counts as off."""
    parameter = f"{settings.agent_kill_switch_prefix}{name}/enabled"
    if not KillSwitch(ssm, parameter).enabled():
        raise GuardError(
            REMEDIATION_DISABLED,
            f"remediation is disabled for agent {name}",
            {"scope": "agent", "agent": name},
        )


def guard_plan(settings: Settings, ssm: Any) -> Agent | None:
    """Plan and request_approval: the agent the plan is for, whose kill switch must be on,
    before any cluster read."""
    agent = resolve_caller(settings)
    if agent is not None:
        check_agent_enabled(ssm, settings, agent.name)
    return agent


def check_agent_budget(store: PlanStore, agent: Agent | None, now: int) -> None:
    """Plan only: the agent's counter for this clock hour, read; apply takes the unit."""
    if agent is not None and store.agent_applies(agent.name, now) >= agent.hourly_apply_budget:
        raise agent_budget_error(
            agent.name, now - now % GLOBAL_BUDGET_WINDOW_SECONDS, agent.hourly_apply_budget
        )


def guard_apply(settings: Settings) -> str | None:
    """The requester the plan must have, for the consume condition. A gateway apply in
    identity mode must come from the plan's own agent. The approval channel carries no
    caller: the approval condition applies there, and the plan's agent is charged."""
    if current_channel() != CHANNEL_GATEWAY:
        return None
    agent = resolve_caller(settings)
    return None if agent is None else requester(agent)


def check_plan_agent(settings: Settings, ssm: Any, plan: Plan) -> None:
    """After the consume, on every channel: in identity mode the plan must name its agent,
    and that agent's kill switch must be on. Checked here because on the approval channel
    the agent is known only from the record. A plan made before identity mode (requested by
    the gateway class) cannot be applied; plan again."""
    if not settings.identity_required:
        return
    if plan.agent is None:
        raise agent_unknown_error("the plan names no agent; plan again")
    check_agent_enabled(ssm, settings, plan.agent)


def take_agent_budget(store: PlanStore, settings: Settings, plan: Plan, now: int) -> None:
    """Apply only, before the global unit: one unit of the plan's agent's hourly budget,
    whichever channel applies it."""
    if settings.identity_required and plan.agent is not None:
        store.take_agent_budget(plan.agent, now, settings.agent_budget(plan.agent))
