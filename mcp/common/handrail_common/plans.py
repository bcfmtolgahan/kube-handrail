"""Single-use plan records in DynamoDB."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from handrail_common.envelope import (
    GLOBAL_BUDGET_EXCEEDED,
    PLAN_EXPIRED,
    PLAN_MISMATCH,
    PLAN_NOT_FOUND,
    PLAN_USED,
)
from handrail_common.validation import GuardError

SCALE_DEPLOYMENT = "scale_deployment"
RESULT_APPLIED = "applied"
# Global secondary index on (object_key, created_at); the same name is set in terraform/main.tf.
OBJECT_HISTORY_INDEX = "object-history"
# Global apply counters share the plan table under keys that can never be a plan id (a UUID).
GLOBAL_BUDGET_PREFIX = "budget#global#"
GLOBAL_BUDGET_WINDOW_SECONDS = 3600


def global_budget_key(now: int) -> str:
    """Counter for the clock hour that contains `now`."""
    return f"{GLOBAL_BUDGET_PREFIX}{now - now % GLOBAL_BUDGET_WINDOW_SECONDS}"


def args_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def scale_args_hash(
    namespace: str, name: str, replicas: int, override_manual_change: bool = False
) -> str:
    payload: dict[str, Any] = {
        "action": SCALE_DEPLOYMENT,
        "namespace": namespace,
        "name": name,
        "replicas": replicas,
    }
    # Only a true override enters the hash, so hashes without it are unchanged.
    if override_manual_change:
        payload["override_manual_change"] = True
    return args_hash(payload)


def object_key(namespace: str, name: str, uid: str) -> str:
    """History key: a Deployment recreated under the same name starts a new history."""
    return f"{namespace}/{name}/{uid}"


@dataclass(frozen=True)
class Plan:
    plan_id: str
    action: str
    namespace: str
    name: str
    replicas_from: int
    replicas_to: int
    args_hash: str
    resource_version: str
    generation: int
    uid: str
    created_at: int
    expires_at: int
    used: bool = False
    # Table TTL attribute: how long the record is kept as history, well past expires_at.
    retain_until: int | None = None
    # Written by apply after the patch (see record_result); absent until then.
    result: str | None = None
    applied_at: int | None = None

    @property
    def object_key(self) -> str:
        return object_key(self.namespace, self.name, self.uid)

    def to_item(self) -> dict[str, Any]:
        item = {
            "plan_id": self.plan_id,
            "action": self.action,
            "namespace": self.namespace,
            "name": self.name,
            "replicas_from": self.replicas_from,
            "replicas_to": self.replicas_to,
            "args_hash": self.args_hash,
            "resource_version": self.resource_version,
            "generation": self.generation,
            "uid": self.uid,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "used": self.used,
            "object_key": self.object_key,
        }
        if self.retain_until is not None:
            item["retain_until"] = self.retain_until
        if self.result is not None:
            item["result"] = self.result
        if self.applied_at is not None:
            item["applied_at"] = self.applied_at
        return item

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> "Plan":
        return cls(
            plan_id=item["plan_id"],
            action=item["action"],
            namespace=item["namespace"],
            name=item["name"],
            replicas_from=int(item["replicas_from"]),
            replicas_to=int(item["replicas_to"]),
            args_hash=item["args_hash"],
            resource_version=item["resource_version"],
            # Plans written before generation and uid were recorded can never match a live object.
            generation=int(item.get("generation", -1)),
            uid=str(item.get("uid", "")),
            created_at=int(item["created_at"]),
            expires_at=int(item["expires_at"]),
            used=bool(item["used"]),
            retain_until=int(item["retain_until"]) if "retain_until" in item else None,
            result=str(item["result"]) if item.get("result") is not None else None,
            applied_at=int(item["applied_at"]) if item.get("applied_at") is not None else None,
        )


@dataclass(frozen=True)
class HistoryEntry:
    """One plan record for an object, as read from the history index."""

    plan_id: str
    replicas_from: int
    replicas_to: int
    created_at: int
    result: str | None
    applied_at: int | None

    @property
    def applied(self) -> bool:
        return self.result == RESULT_APPLIED and self.applied_at is not None

    @classmethod
    def from_item(cls, item: Mapping[str, Any]) -> "HistoryEntry":
        applied_at = item.get("applied_at")
        result = item.get("result")
        return cls(
            plan_id=str(item["plan_id"]),
            replicas_from=int(item["replicas_from"]),
            replicas_to=int(item["replicas_to"]),
            created_at=int(item["created_at"]),
            result=str(result) if result is not None else None,
            applied_at=int(applied_at) if applied_at is not None else None,
        )


def _is_conditional_failure(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"


class PlanStore:
    def __init__(self, table: Any) -> None:
        self._table = table

    @classmethod
    def from_name(cls, table_name: str, region: str | None = None) -> "PlanStore":
        return cls(boto3.resource("dynamodb", region_name=region).Table(table_name))

    def create(self, plan: Plan) -> None:
        self._table.put_item(
            Item=plan.to_item(),
            ConditionExpression="attribute_not_exists(plan_id)",
        )

    def consume(self, plan_id: str, expected_hash: str, action: str, now: int) -> Plan:
        """Atomically mark the plan used; raise GuardError with the precise reason otherwise."""
        try:
            response = self._table.update_item(
                Key={"plan_id": plan_id},
                UpdateExpression="SET #used = :true",
                ConditionExpression=(
                    "#used = :false AND expires_at > :now AND args_hash = :h AND #action = :a"
                ),
                ExpressionAttributeNames={"#used": "used", "#action": "action"},
                ExpressionAttributeValues={
                    ":true": True,
                    ":false": False,
                    ":now": now,
                    ":h": expected_hash,
                    ":a": action,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise
            raise self._rejection(plan_id, expected_hash, action, now) from None
        return Plan.from_item(response["Attributes"])

    def consumed_plan(self, plan_id: str) -> Plan | None:
        """The plan if it is in the table and an apply has consumed it, otherwise None.

        Strongly consistent. The recorded `result` is deliberately not required: the caller
        found the id in the Deployment's last-plan annotation, which only a successful patch
        sets, so the plan was applied even if its result was never recorded.
        """
        item = self._table.get_item(Key={"plan_id": plan_id}, ConsistentRead=True).get("Item")
        if item is None or item.get("used") is not True:
            return None
        return Plan.from_item(item)

    def history(self, key: str, since: int) -> list[HistoryEntry]:
        """Plans for one object created at or after `since`, oldest first.

        Errors propagate: a check that cannot read the history must not pass. A global
        secondary index is eventually consistent, so a write from the last moment may be missing.
        """
        query: dict[str, Any] = {
            "IndexName": OBJECT_HISTORY_INDEX,
            "KeyConditionExpression": "object_key = :k AND created_at >= :since",
            "ExpressionAttributeValues": {":k": key, ":since": since},
        }
        entries: list[HistoryEntry] = []
        while True:
            response = self._table.query(**query)
            entries.extend(HistoryEntry.from_item(item) for item in response.get("Items", []))
            last = response.get("LastEvaluatedKey")
            if not last:
                break
            query["ExclusiveStartKey"] = last
        return sorted(entries, key=lambda e: (e.created_at, e.plan_id))

    def record_result(self, plan_id: str, applied_at: int, result: str) -> None:
        self._table.update_item(
            Key={"plan_id": plan_id},
            UpdateExpression="SET applied_at = :t, #result = :r",
            ExpressionAttributeNames={"#result": "result"},
            ExpressionAttributeValues={":t": applied_at, ":r": result},
        )

    def global_applies(self, now: int) -> int:
        """Applies counted so far in the current clock hour, read consistently."""
        item = self._table.get_item(
            Key={"plan_id": global_budget_key(now)}, ConsistentRead=True
        ).get("Item")
        return int(item.get("applies", 0)) if item else 0

    def take_global_budget(self, now: int, limit: int) -> None:
        """Count one apply against the global hourly budget, or raise if it is spent.

        One conditional update, so concurrent applies cannot overshoot the limit. The unit
        is taken before the patch and not given back if the apply fails afterwards.
        """
        start = now - now % GLOBAL_BUDGET_WINDOW_SECONDS
        try:
            self._table.update_item(
                Key={"plan_id": global_budget_key(now)},
                UpdateExpression="ADD applies :one SET retain_until = :retain",
                ConditionExpression="attribute_not_exists(applies) OR applies < :limit",
                ExpressionAttributeValues={
                    ":one": 1,
                    ":limit": limit,
                    ":retain": start + 2 * GLOBAL_BUDGET_WINDOW_SECONDS,
                },
            )
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise
            raise global_budget_error(start, limit) from None

    def _rejection(self, plan_id: str, expected_hash: str, action: str, now: int) -> GuardError:
        item = self._table.get_item(Key={"plan_id": plan_id}, ConsistentRead=True).get("Item")
        if item is None:
            return GuardError(PLAN_NOT_FOUND, "plan not found")
        if item.get("used") is True:
            return GuardError(PLAN_USED, "plan already used")
        if Decimal(item.get("expires_at", 0)) <= now:
            return GuardError(PLAN_EXPIRED, "plan expired")
        if item.get("action") != action or item.get("args_hash") != expected_hash:
            return GuardError(PLAN_MISMATCH, "arguments do not match the plan")
        # The record satisfies every condition now, so it changed concurrently; refuse.
        return GuardError(PLAN_USED, "plan was consumed concurrently")


def global_budget_error(window_start: int, limit: int) -> GuardError:
    return GuardError(
        GLOBAL_BUDGET_EXCEEDED,
        f"the global limit of {limit} applies in this clock hour has been reached",
        {
            "limit": limit,
            "window_start": window_start,
            "retry_after": window_start + GLOBAL_BUDGET_WINDOW_SECONDS,
        },
    )
