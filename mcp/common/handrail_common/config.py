"""Typed settings from the environment variables listed in mcp/CONTRACT.md."""

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(Exception):
    pass


CLIENT = "client"
SERVER = "server"
BOTH = "both"
APPROVAL_MODES = (CLIENT, SERVER, BOTH)


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
    plan_rate_limit: int = 20
    plan_rate_window_seconds: int = 600
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

    @property
    def approval_required(self) -> bool:
        """In both mode the client approval is invisible here, so the server acts as in server."""
        return self.approval_mode != CLIENT

    def bounds_for(self, namespace: str) -> tuple[int, int]:
        """(minimum, maximum) replicas in an allowed namespace. Every replica bounds check
        reads them through here; today the same bounds apply to every namespace."""
        return self.min_replicas, self.max_replicas


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

    ttl = int_env(env, "PLAN_TTL_SECONDS", 300)
    if ttl <= 0:
        raise ConfigError("PLAN_TTL_SECONDS must be positive")

    limits = {
        key: int_env(env, key, default)
        for key, default in (
            ("FLAP_WINDOW_SECONDS", 600),
            ("OBJECT_HOURLY_APPLY_BUDGET", 6),
            ("PLAN_RATE_LIMIT", 20),
            ("PLAN_RATE_WINDOW_SECONDS", 600),
            ("APPLY_COOLDOWN_SECONDS", 60),
            ("GLOBAL_HOURLY_APPLY_BUDGET", 30),
            ("MAX_REPLICA_STEP_PERCENT", 100),
            ("APPROVAL_WAIT_TTL_SECONDS", 900),
            ("APPLY_AFTER_APPROVAL_TTL_SECONDS", 120),
            ("APPROVAL_REQUEST_COOLDOWN_SECONDS", 60),
            ("APPROVAL_REQUEST_LIMIT", 10),
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

    param = env.get("REMEDIATION_ENABLED_PARAM", "").strip() or None
    if write and param is None:
        raise ConfigError("REMEDIATION_ENABLED_PARAM is required")

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
        plan_rate_limit=limits["PLAN_RATE_LIMIT"],
        plan_rate_window_seconds=limits["PLAN_RATE_WINDOW_SECONDS"],
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
    )
