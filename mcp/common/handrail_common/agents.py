"""Per-agent identity (mcp/CONTRACT.md, Agent identity): which agent asked for a plan, and
which may request its approval or apply it.

Every plan_* and apply_* tool runs through the generic plan and apply paths, which call
guard_plan and guard_apply, so a new action cannot skip them.
"""

from handrail_common.config import Agent, Settings
from handrail_common.envelope import FORBIDDEN
from handrail_common.observability import CHANNEL_GATEWAY, current_caller, current_channel
from handrail_common.plans import REQUESTER_GATEWAY, Plan
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


def guard_plan(settings: Settings) -> Agent | None:
    """Plan and request_approval: the agent the plan is for, before any cluster read."""
    return resolve_caller(settings)


def guard_apply(settings: Settings) -> str | None:
    """The requester the plan must have, for the consume condition. A gateway apply in
    identity mode must come from the plan's own agent. The approval channel carries no
    caller: the approval condition applies there, and the plan's agent is charged."""
    if current_channel() != CHANNEL_GATEWAY:
        return None
    agent = resolve_caller(settings)
    return None if agent is None else requester(agent)


def check_plan_agent(settings: Settings, plan: Plan) -> None:
    """After the consume: in identity mode the plan must name its agent. A plan made before
    identity mode (requested by the gateway class) cannot be applied; plan again."""
    if settings.identity_required and plan.agent is None:
        raise agent_unknown_error("the plan names no agent; plan again")
