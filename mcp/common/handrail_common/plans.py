"""Single-use plan records in DynamoDB."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from handrail_common.envelope import PLAN_EXPIRED, PLAN_MISMATCH, PLAN_NOT_FOUND, PLAN_USED
from handrail_common.validation import GuardError

SCALE_DEPLOYMENT = "scale_deployment"


def args_hash(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def scale_args_hash(namespace: str, name: str, replicas: int) -> str:
    return args_hash(
        {"action": SCALE_DEPLOYMENT, "namespace": namespace, "name": name, "replicas": replicas}
    )


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

    def to_item(self) -> dict[str, Any]:
        return {
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
        }

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

    def record_result(self, plan_id: str, applied_at: int, result: str) -> None:
        self._table.update_item(
            Key={"plan_id": plan_id},
            UpdateExpression="SET applied_at = :t, #result = :r",
            ExpressionAttributeNames={"#result": "result"},
            ExpressionAttributeValues={":t": applied_at, ":r": result},
        )

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
