"""Typed settings from the environment variables listed in mcp/CONTRACT.md."""

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from handrail_common.gitops import valid_marker


class ConfigError(Exception):
    pass


CLIENT = "client"
SERVER = "server"
BOTH = "both"
APPROVAL_MODES = (CLIENT, SERVER, BOTH)
# The keys of the Terraform agents map; also part of SSM parameter names and counter keys.
AGENT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# A plan is a snapshot of the cluster; an hour-old plan is approved against a stale view.
MAX_PLAN_TTL_SECONDS = 3600
# The verifier runs every minute, so a shorter window could not be kept.
MIN_VERIFY_WINDOW_SECONDS = 60
# The actions a namespace can allow (Terraform namespaces[*].actions); each is the plan
# record's action without the _deployment suffix.
ACTIONS = ("restart", "rollback", "scale")
# The namespace policy Terraform renders into every Lambda package, next to handrail_common
# (mcp/CONTRACT.md, Namespace policy). NAMESPACE_POLICY_FILE overrides the path (tests).
POLICY_FILE_NAME = "namespace_policy.json"
DEFAULT_POLICY_FILE = Path(__file__).resolve().parent.parent / POLICY_FILE_NAME
_NAMESPACE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


@dataclass(frozen=True)
class Agent:
    """One agent: an OAuth client listed in AGENTS (mcp/CONTRACT.md, Agent identity)."""

    name: str
    client_id: str
    hourly_apply_budget: int


@dataclass(frozen=True)
class NamespacePolicy:
    """What one namespace allows (mcp/CONTRACT.md, Namespace policy)."""

    min_replicas: int
    max_replicas: int
    actions: frozenset[str] = frozenset(ACTIONS)
    # IAM user or role ARNs that decide this namespace's plans; empty means the global
    # APPROVER_PRINCIPALS. Checked by the approver only.
    approvers: tuple[str, ...] = ()
    # GitOps marker keys checked in this namespace in addition to GITOPS_MARKERS.
    gitops_markers: tuple[str, ...] = ()


def action_name(plan_action: str) -> str:
    """scale_deployment => scale: the name a namespace policy allows."""
    return plan_action.removesuffix("_deployment")


@dataclass(frozen=True)
class Settings:
    cluster_name: str
    plans_table: str
    allowed_namespaces: frozenset[str]
    min_replicas: int
    max_replicas: int
    plan_ttl_seconds: int
    remediation_enabled_param: str | None
    region: str | None
    flap_window_seconds: int = 600
    object_hourly_apply_budget: int = 6
    apply_cooldown_seconds: int = 60
    global_hourly_apply_budget: int = 30
    max_replica_step_percent: int = 100
    # Unset means server: without an explicit choice, apply requires a server approval.
    approval_mode: str = SERVER
    approval_wait_ttl_seconds: int = 900
    apply_after_approval_ttl_seconds: int = 120
    approval_request_cooldown_seconds: int = 60
    approval_request_limit: int = 10
    # Read Lambda only: where request_approval notifies, and the URL shown in the message.
    approval_topic_arn: str | None = None
    approval_url: str | None = None
    # client_id => Agent, from AGENTS: callers are verified OAuth clients (the gateway
    # interceptor), and planning, request_approval and gateway applies need one listed here.
    agents: Mapping[str, Agent] = field(default_factory=dict)
    # Per-agent kill switches are SSM parameters <prefix><name>/enabled.
    agent_kill_switch_prefix: str | None = None
    # Applies per agent in one clock hour, unless AGENTS sets the agent's own.
    agent_hourly_apply_budget: int = 10
    # Every apply schedules a verification (mcp/CONTRACT.md, Verification and revert), and
    # plans may pin a window and revert_on_failure.
    verify_window_default_seconds: int = 120
    verify_window_min_seconds: int = 60
    verify_window_max_seconds: int = 1800
    # How long after verify_at a verification may still run; later it is unknown (late).
    verify_grace_seconds: int = 300
    # The SSM parameter holding the change freezes (mcp/CONTRACT.md, Change freezes); unset
    # means no freezes are checked.
    freezes_param: str | None = None
    # Label or annotation keys that mark GitOps ownership in addition to the defaults
    # (handrail_common.gitops), from GITOPS_MARKERS.
    gitops_markers: tuple[str, ...] = ()
    # Namespace => policy, from namespace_policy.json. Empty when the package has no policy
    # file (an older build, a test): every allowed namespace then allows every action
    # within MIN_REPLICAS and MAX_REPLICAS.
    namespace_policy: Mapping[str, NamespacePolicy] = field(default_factory=dict)
    # Read Lambda: compare the cluster's admission policy with the bounds on every plan
    # (mcp/CONTRACT.md, Admission policy). Off only through ADMISSION_CHECK=false.
    admission_check: bool = True

    @property
    def approval_required(self) -> bool:
        """In both mode the client approval is invisible here, so the server acts as in server."""
        return self.approval_mode != CLIENT

    def bounds_for(self, namespace: str) -> tuple[int, int]:
        """(minimum, maximum) replicas in an allowed namespace. Every replica bounds check
        reads them through here: the namespace's policy, else MIN_REPLICAS and MAX_REPLICAS."""
        policy = self.namespace_policy.get(namespace)
        if policy is None:
            return self.min_replicas, self.max_replicas
        return policy.min_replicas, policy.max_replicas

    def actions_for(self, namespace: str) -> frozenset[str]:
        """The actions (ACTIONS) an allowed namespace allows; every action without a policy."""
        policy = self.namespace_policy.get(namespace)
        return frozenset(ACTIONS) if policy is None else policy.actions

    def markers_for(self, namespace: str) -> tuple[str, ...]:
        """GITOPS_MARKERS and the namespace's own marker keys."""
        policy = self.namespace_policy.get(namespace)
        extra = () if policy is None else policy.gitops_markers
        return tuple(dict.fromkeys((*self.gitops_markers, *extra)))

    @property
    def verify_window_bounds(self) -> tuple[int, int]:
        return self.verify_window_min_seconds, self.verify_window_max_seconds

    def agent_budget(self, name: str) -> int:
        """The hourly apply budget of the agent with this name. An agent no longer in AGENTS
        (its plan was made before it was removed) gets the default."""
        budgets = {a.name: a.hourly_apply_budget for a in self.agents.values()}
        return budgets.get(name, self.agent_hourly_apply_budget)


def required_env(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is required")
    return value


def int_env(env: Mapping[str, str], key: str, default: int | None = None) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"{key} is required")
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer") from None


def approval_mode(env: Mapping[str, str]) -> str:
    mode = env.get("APPROVAL_MODE", "").strip() or SERVER
    if mode not in APPROVAL_MODES:
        raise ConfigError(f"APPROVAL_MODE must be one of {', '.join(APPROVAL_MODES)}")
    return mode


def parse_gitops_markers(raw: str) -> tuple[str, ...]:
    """GITOPS_MARKERS: a JSON array of label or annotation keys; unset or empty is none.
    Anything else fails the load, so a typo cannot silently turn the guard off."""
    if not raw.strip():
        return ()
    try:
        value = json.loads(raw)
    except ValueError:
        raise ConfigError("GITOPS_MARKERS must be a JSON array of strings") from None
    if not isinstance(value, list) or not all(valid_marker(key) for key in value):
        raise ConfigError("GITOPS_MARKERS must be a JSON array of label or annotation keys")
    return tuple(dict.fromkeys(value))


def _int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v for v in value)


def parse_namespace_policy(raw: str) -> dict[str, NamespacePolicy]:
    """namespace_policy.json: {"namespaces": {"<ns>": {"min_replicas": N, "max_replicas": M,
    "actions": [...], "approvers": [...], "gitops_markers": [...]}}}, at least one namespace.
    Anything else fails the load: the file widens access, so a malformed one must not
    silently fall back to anything."""
    try:
        value = json.loads(raw)
    except ValueError:
        raise ConfigError(f"{POLICY_FILE_NAME} must be a JSON object") from None
    namespaces = value.get("namespaces") if isinstance(value, dict) else None
    if not isinstance(namespaces, dict) or not namespaces:
        raise ConfigError(f"{POLICY_FILE_NAME} must list at least one namespace")
    policy: dict[str, NamespacePolicy] = {}
    for name, entry in namespaces.items():
        where = f"{POLICY_FILE_NAME}: namespace {name!r}"
        if not isinstance(name, str) or len(name) > 63 or not _NAMESPACE.match(name):
            raise ConfigError(f"{where} is not a DNS-1123 label")
        if not isinstance(entry, dict):
            raise ConfigError(f"{where} must be an object")
        minimum, maximum = entry.get("min_replicas"), entry.get("max_replicas")
        if not (_int(minimum) and _int(maximum) and 0 <= minimum <= maximum):
            raise ConfigError(f"{where}: require 0 <= min_replicas <= max_replicas")
        actions = entry.get("actions")
        if not _strings(actions) or not set(actions) <= set(ACTIONS):
            raise ConfigError(f"{where}: actions must be a list of {', '.join(ACTIONS)}")
        approvers = entry.get("approvers", [])
        if not isinstance(approvers, list) or not (approvers == [] or _strings(approvers)):
            raise ConfigError(f"{where}: approvers must be a list of ARNs")
        markers = entry.get("gitops_markers", [])
        if not isinstance(markers, list) or not all(valid_marker(key) for key in markers):
            raise ConfigError(f"{where}: gitops_markers must be label or annotation keys")
        policy[name] = NamespacePolicy(
            min_replicas=minimum,
            max_replicas=maximum,
            actions=frozenset(actions),
            approvers=tuple(dict.fromkeys(approvers)),
            gitops_markers=tuple(dict.fromkeys(markers)),
        )
    return policy


def policy_path(env: Mapping[str, str]) -> Path:
    return Path(env.get("NAMESPACE_POLICY_FILE", "").strip() or DEFAULT_POLICY_FILE)


def load_namespace_policy(env: Mapping[str, str]) -> dict[str, NamespacePolicy]:
    """The packaged namespace policy, or {} when the package has none."""
    path = policy_path(env)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if env.get("NAMESPACE_POLICY_FILE", "").strip():
            # Named explicitly: a missing file is a mistake, not an older package.
            raise ConfigError(f"NAMESPACE_POLICY_FILE: {path} does not exist") from None
        return {}
    return parse_namespace_policy(raw)


def parse_admission_check(env: Mapping[str, str]) -> bool:
    """ADMISSION_CHECK: only the exact value "false" turns the check off; unset is on."""
    raw = env.get("ADMISSION_CHECK", "").strip()
    if raw not in ("", "true", "false"):
        raise ConfigError("ADMISSION_CHECK must be true or false")
    return raw != "false"


def parse_agents(raw: str, default_budget: int) -> dict[str, Agent]:
    """AGENTS: a JSON object {"<client_id>": {"name": "<agent>", "hourly_apply_budget": N}},
    at least one entry, with unique names; the budget is optional and positive."""
    try:
        value = json.loads(raw)
    except ValueError:
        raise ConfigError("AGENTS must be a JSON object") from None
    if not isinstance(value, dict) or not value:
        raise ConfigError("AGENTS must list at least one agent")
    agents: dict[str, Agent] = {}
    for client_id, entry in value.items():
        name = entry.get("name") if isinstance(entry, dict) else None
        if not client_id or not isinstance(name, str) or not AGENT_NAME.match(name):
            raise ConfigError(f"AGENTS: each agent needs a name matching {AGENT_NAME.pattern}")
        budget = entry.get("hourly_apply_budget", default_budget)
        if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
            raise ConfigError(f"AGENTS: {name}: hourly_apply_budget must be a positive integer")
        agents[client_id] = Agent(name=name, client_id=client_id, hourly_apply_budget=budget)
    if len({a.name for a in agents.values()}) != len(agents):
        raise ConfigError("AGENTS: agent names must be unique")
    return agents


def load_settings(env: Mapping[str, str] | None = None, *, write: bool = False) -> Settings:
    """Load and validate settings. `write=True` also requires REMEDIATION_ENABLED_PARAM."""
    env = os.environ if env is None else env
    namespaces = frozenset(
        ns.strip() for ns in required_env(env, "ALLOWED_NAMESPACES").split(",") if ns.strip()
    )
    if not namespaces:
        raise ConfigError("ALLOWED_NAMESPACES must list at least one namespace")

    min_replicas = int_env(env, "MIN_REPLICAS")
    max_replicas = int_env(env, "MAX_REPLICAS")
    if min_replicas < 0 or max_replicas < min_replicas:
        raise ConfigError("require 0 <= MIN_REPLICAS <= MAX_REPLICAS")

    # Terraform derives ALLOWED_NAMESPACES from the same map as the policy file; a package
    # and an environment from different applies must not run together.
    namespace_policy = load_namespace_policy(env)
    if namespace_policy and set(namespace_policy) != namespaces:
        raise ConfigError(f"ALLOWED_NAMESPACES does not match the namespaces of {POLICY_FILE_NAME}")

    ttl = int_env(env, "PLAN_TTL_SECONDS", 300)
    if not 0 < ttl <= MAX_PLAN_TTL_SECONDS:
        raise ConfigError(f"PLAN_TTL_SECONDS must be between 1 and {MAX_PLAN_TTL_SECONDS}")

    limits = {
        key: int_env(env, key, default)
        for key, default in (
            ("FLAP_WINDOW_SECONDS", 600),
            ("OBJECT_HOURLY_APPLY_BUDGET", 6),
            ("APPLY_COOLDOWN_SECONDS", 60),
            ("GLOBAL_HOURLY_APPLY_BUDGET", 30),
            ("MAX_REPLICA_STEP_PERCENT", 100),
            ("APPROVAL_WAIT_TTL_SECONDS", 900),
            ("APPLY_AFTER_APPROVAL_TTL_SECONDS", 120),
            ("APPROVAL_REQUEST_COOLDOWN_SECONDS", 60),
            ("APPROVAL_REQUEST_LIMIT", 10),
            ("AGENT_HOURLY_APPLY_BUDGET", 10),
            ("VERIFY_WINDOW_DEFAULT_SECONDS", 120),
            ("VERIFY_WINDOW_MIN_SECONDS", 60),
            ("VERIFY_WINDOW_MAX_SECONDS", 1800),
            ("VERIFY_GRACE_SECONDS", 300),
        )
    }
    may_be_zero = ("APPLY_COOLDOWN_SECONDS", "APPROVAL_REQUEST_COOLDOWN_SECONDS")
    for key, value in limits.items():
        # A cooldown of 0 turns it off; every other limit must be positive.
        if value < 0 or (value == 0 and key not in may_be_zero):
            raise ConfigError(f"{key} must be positive")
    # Otherwise requesting an approval would shorten the plan's validity.
    if limits["APPROVAL_WAIT_TTL_SECONDS"] < ttl:
        raise ConfigError("APPROVAL_WAIT_TTL_SECONDS must be at least PLAN_TTL_SECONDS")
    if not (
        MIN_VERIFY_WINDOW_SECONDS
        <= limits["VERIFY_WINDOW_MIN_SECONDS"]
        <= limits["VERIFY_WINDOW_DEFAULT_SECONDS"]
        <= limits["VERIFY_WINDOW_MAX_SECONDS"]
    ):
        raise ConfigError(
            f"require {MIN_VERIFY_WINDOW_SECONDS} <= VERIFY_WINDOW_MIN_SECONDS <= "
            "VERIFY_WINDOW_DEFAULT_SECONDS <= VERIFY_WINDOW_MAX_SECONDS"
        )

    param = env.get("REMEDIATION_ENABLED_PARAM", "").strip() or None
    if write and param is None:
        raise ConfigError("REMEDIATION_ENABLED_PARAM is required")

    # Every caller is an agent: without AGENTS or the switch prefix the Lambda does not start.
    default_budget = limits["AGENT_HOURLY_APPLY_BUDGET"]
    agents = parse_agents(required_env(env, "AGENTS"), default_budget)
    switch_prefix = required_env(env, "AGENT_KILL_SWITCH_PREFIX")

    return Settings(
        cluster_name=required_env(env, "CLUSTER_NAME"),
        plans_table=required_env(env, "PLANS_TABLE"),
        allowed_namespaces=namespaces,
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        plan_ttl_seconds=ttl,
        remediation_enabled_param=param,
        region=env.get("AWS_REGION") or None,
        flap_window_seconds=limits["FLAP_WINDOW_SECONDS"],
        object_hourly_apply_budget=limits["OBJECT_HOURLY_APPLY_BUDGET"],
        apply_cooldown_seconds=limits["APPLY_COOLDOWN_SECONDS"],
        global_hourly_apply_budget=limits["GLOBAL_HOURLY_APPLY_BUDGET"],
        max_replica_step_percent=limits["MAX_REPLICA_STEP_PERCENT"],
        approval_mode=approval_mode(env),
        approval_wait_ttl_seconds=limits["APPROVAL_WAIT_TTL_SECONDS"],
        apply_after_approval_ttl_seconds=limits["APPLY_AFTER_APPROVAL_TTL_SECONDS"],
        approval_request_cooldown_seconds=limits["APPROVAL_REQUEST_COOLDOWN_SECONDS"],
        approval_request_limit=limits["APPROVAL_REQUEST_LIMIT"],
        approval_topic_arn=env.get("APPROVAL_TOPIC_ARN", "").strip() or None,
        approval_url=env.get("APPROVAL_URL", "").strip() or None,
        agents=agents,
        agent_kill_switch_prefix=switch_prefix,
        agent_hourly_apply_budget=default_budget,
        verify_window_default_seconds=limits["VERIFY_WINDOW_DEFAULT_SECONDS"],
        verify_window_min_seconds=limits["VERIFY_WINDOW_MIN_SECONDS"],
        verify_window_max_seconds=limits["VERIFY_WINDOW_MAX_SECONDS"],
        verify_grace_seconds=limits["VERIFY_GRACE_SECONDS"],
        freezes_param=env.get("FREEZES_PARAM", "").strip() or None,
        gitops_markers=parse_gitops_markers(env.get("GITOPS_MARKERS", "")),
        namespace_policy=namespace_policy,
        admission_check=parse_admission_check(env),
    )
