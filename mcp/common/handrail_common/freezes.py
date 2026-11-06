"""Change freezes (mcp/CONTRACT.md, Change freezes): windows in which no change is planned or
applied in the namespaces they name.

The windows are a JSON list in one SSM String parameter (FREEZES_PARAM), read on every check
like the kill switch. A parameter that cannot be read or does not validate refuses every
write, as an unreadable kill switch does. scripts/freeze.py writes the parameter through
parse_freezes, so an operator cannot store what the Lambdas would refuse.
"""

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from handrail_common.envelope import CHANGE_FROZEN, REMEDIATION_DISABLED
from handrail_common.validation import GuardError

logger = logging.getLogger(__name__)

# Action names in a freeze entry => the plan record's `action`.
ACTIONS = {
    "scale": "scale_deployment",
    "restart": "restart_deployment",
    "rollback": "rollback_deployment",
}
ALL_NAMESPACES = "*"
# A standard SSM parameter holds 4 KB; 50 short entries fit.
MAX_FREEZES = 50
MAX_REASON_CHARS = 200
FIELDS = frozenset({"namespaces", "start", "until", "reason", "actions"})
REQUIRED = frozenset({"namespaces", "start", "until", "reason"})
# RFC 3339 in UTC, to the second: 2026-12-23T00:00:00Z.
# fullmatch, not match: "$" also matches before a trailing newline.
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_DNS1123_LABEL = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
# details.reason of remediation_disabled when the freeze parameter cannot be used.
UNREADABLE = "unreadable"
INVALID = "invalid"
SCOPE = "freezes"


class FreezeConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Freeze:
    namespaces: tuple[str, ...]
    # Epoch seconds: start inclusive, until exclusive.
    start: int
    until: int
    reason: str
    # Plan record actions; None means every action.
    actions: frozenset[str] | None = None

    def covers(self, namespace: str, action: str) -> bool:
        in_namespace = ALL_NAMESPACES in self.namespaces or namespace in self.namespaces
        return in_namespace and (self.actions is None or action in self.actions)

    def active(self, now: int) -> bool:
        return self.start <= now < self.until

    def to_json(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "namespaces": list(self.namespaces),
            "start": format_time(self.start),
            "until": format_time(self.until),
            "reason": self.reason,
        }
        if self.actions is not None:
            names = {plan: short for short, plan in ACTIONS.items()}
            entry["actions"] = sorted(names[a] for a in self.actions)
        return entry


def parse_time(value: Any, field: str) -> int:
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        raise FreezeConfigError(f"{field} must be RFC 3339 in UTC, like 2026-12-23T00:00:00Z")
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise FreezeConfigError(f"{field} is not a valid date and time") from None
    return int(moment.timestamp())


def format_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _namespaces(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise FreezeConfigError("namespaces must be a non-empty list")
    for ns in value:
        valid_label = isinstance(ns, str) and len(ns) <= 63 and _DNS1123_LABEL.fullmatch(ns)
        if ns != ALL_NAMESPACES and not valid_label:
            raise FreezeConfigError(
                f"namespaces: {ns!r} is not a DNS-1123 label or {ALL_NAMESPACES!r}"
            )
    if len(set(value)) != len(value):
        raise FreezeConfigError("namespaces must not repeat")
    return tuple(value)


def _actions(value: Any) -> frozenset[str]:
    if not isinstance(value, list) or not value:
        raise FreezeConfigError("actions must be a non-empty list when given")
    for action in value:
        if action not in ACTIONS:
            raise FreezeConfigError(f"actions: {action!r} is not one of {', '.join(ACTIONS)}")
    if len(set(value)) != len(value):
        raise FreezeConfigError("actions must not repeat")
    return frozenset(ACTIONS[a] for a in value)


def _reason(value: Any) -> str:
    # Returned to agents and approvers as is: plain printable text only.
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_REASON_CHARS
        or not value.isprintable()
    ):
        raise FreezeConfigError(
            f"reason must be printable text of 1 to {MAX_REASON_CHARS} characters"
        )
    return value


def parse_entry(entry: Any) -> Freeze:
    if not isinstance(entry, dict):
        raise FreezeConfigError("each freeze must be a JSON object")
    unknown = sorted(set(entry) - FIELDS)
    if unknown:
        raise FreezeConfigError(f"unknown fields: {', '.join(unknown)}")
    missing = sorted(REQUIRED - set(entry))
    if missing:
        raise FreezeConfigError(f"missing fields: {', '.join(missing)}")
    start = parse_time(entry["start"], "start")
    until = parse_time(entry["until"], "until")
    if until <= start:
        raise FreezeConfigError("until must be later than start")
    return Freeze(
        namespaces=_namespaces(entry["namespaces"]),
        start=start,
        until=until,
        reason=_reason(entry["reason"]),
        actions=_actions(entry["actions"]) if "actions" in entry else None,
    )


def parse_freezes(raw: str) -> list[Freeze]:
    """The whole parameter value. Entries whose window has passed are valid, only inactive,
    so a stale entry never blocks anything."""
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise FreezeConfigError("the freezes must be a JSON list") from None
    if not isinstance(value, list):
        raise FreezeConfigError("the freezes must be a JSON list")
    if len(value) > MAX_FREEZES:
        raise FreezeConfigError(f"at most {MAX_FREEZES} freezes")
    freezes = []
    for i, entry in enumerate(value):
        try:
            freezes.append(parse_entry(entry))
        except FreezeConfigError as exc:
            raise FreezeConfigError(f"freeze {i}: {exc}") from None
    return freezes


def dump_freezes(freezes: Iterable[Freeze]) -> str:
    return json.dumps([f.to_json() for f in freezes], separators=(",", ":"))


def active_freeze(
    freezes: Sequence[Freeze], namespace: str, action: str, now: int
) -> tuple[Freeze, int] | None:
    """The freeze in force for this namespace and action at `now`, and when the frozen
    period ends: windows that overlap or touch it extend it, so `until` is the first moment
    a change is allowed again."""
    matching = [f for f in freezes if f.covers(namespace, action)]
    current = [f for f in matching if f.active(now)]
    if not current:
        return None
    freeze = max(current, key=lambda f: f.until)
    until = freeze.until
    extended = True
    while extended:
        extended = False
        for f in matching:
            if f.start <= until < f.until:
                until, extended = f.until, True
    return freeze, until


class FreezeStore:
    """Reads the parameter on every check, so a freeze takes effect immediately."""

    def __init__(self, ssm_client: Any, parameter_name: str) -> None:
        self._ssm = ssm_client
        self._name = parameter_name

    def load(self) -> list[Freeze]:
        try:
            response = self._ssm.get_parameter(Name=self._name)
            raw = response["Parameter"]["Value"]
        except Exception:
            logger.exception("cannot read the freezes parameter")
            raise GuardError(
                REMEDIATION_DISABLED,
                "the change freezes cannot be read",
                {"scope": SCOPE, "reason": UNREADABLE},
            ) from None
        try:
            return parse_freezes(raw)
        except FreezeConfigError as exc:
            logger.error("invalid freezes parameter: %s", exc)
            raise GuardError(
                REMEDIATION_DISABLED,
                "the change freezes are not valid",
                {"scope": SCOPE, "reason": INVALID},
            ) from None

    def check(self, namespace: str, action: str, now: int) -> None:
        found = active_freeze(self.load(), namespace, action, now)
        if found is None:
            return
        freeze, until = found
        raise GuardError(
            CHANGE_FROZEN,
            f"changes in namespace {namespace} are frozen until {format_time(until)}",
            {"namespace": namespace, "reason": freeze.reason, "until": format_time(until)},
        )


def freeze_store(ssm_client: Any, parameter_name: str | None) -> FreezeStore | None:
    return None if parameter_name is None else FreezeStore(ssm_client, parameter_name)


def check_not_frozen(store: FreezeStore | None, namespace: str, action: str, now: int) -> None:
    """Plan and apply, on every channel; without FREEZES_PARAM there are no freezes."""
    if store is not None:
        store.check(namespace, action, now)
