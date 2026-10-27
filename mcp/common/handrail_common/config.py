"""Typed settings from the environment variables listed in mcp/CONTRACT.md."""

import os
from collections.abc import Mapping
from dataclasses import dataclass


class ConfigError(Exception):
    pass


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


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"{key} is required")
    return value


def _int(env: Mapping[str, str], key: str, default: int | None = None) -> int:
    raw = env.get(key, "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"{key} is required")
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{key} must be an integer") from None


def load_settings(env: Mapping[str, str] | None = None, *, write: bool = False) -> Settings:
    """Load and validate settings. `write=True` also requires REMEDIATION_ENABLED_PARAM."""
    env = os.environ if env is None else env
    namespaces = frozenset(
        ns.strip() for ns in _required(env, "ALLOWED_NAMESPACES").split(",") if ns.strip()
    )
    if not namespaces:
        raise ConfigError("ALLOWED_NAMESPACES must list at least one namespace")

    min_replicas = _int(env, "MIN_REPLICAS")
    max_replicas = _int(env, "MAX_REPLICAS")
    if min_replicas < 0 or max_replicas < min_replicas:
        raise ConfigError("require 0 <= MIN_REPLICAS <= MAX_REPLICAS")

    ttl = _int(env, "PLAN_TTL_SECONDS", 300)
    if ttl <= 0:
        raise ConfigError("PLAN_TTL_SECONDS must be positive")

    param = env.get("REMEDIATION_ENABLED_PARAM", "").strip() or None
    if write and param is None:
        raise ConfigError("REMEDIATION_ENABLED_PARAM is required")

    return Settings(
        cluster_name=_required(env, "CLUSTER_NAME"),
        plans_table=_required(env, "PLANS_TABLE"),
        allowed_namespaces=namespaces,
        min_replicas=min_replicas,
        max_replicas=max_replicas,
        plan_ttl_seconds=ttl,
        remediation_enabled_param=param,
        region=env.get("AWS_REGION") or None,
    )
