"""Single-use plan records in DynamoDB."""

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from handrail_common.envelope import (
    APPROVAL_RATE_LIMITED,
    APPROVAL_REQUIRED,
    CONFLICT,
    FORBIDDEN,
    GLOBAL_BUDGET_EXCEEDED,
    PLAN_DECIDED,
    PLAN_EXPIRED,
    PLAN_MISMATCH,
    PLAN_NOT_FOUND,
    PLAN_USED,
)
from handrail_common.validation import GuardError

SCALE_DEPLOYMENT = "scale_deployment"
RESTART_DEPLOYMENT = "restart_deployment"
ROLLBACK_DEPLOYMENT = "rollback_deployment"
# Pod-template annotation a restart sets to its plan id: the template it leaves is known when
# the plan is made, and every pod it creates names the plan.
RESTART_ANNOTATION = "kube-handrail.io/restart-plan-id"
RESULT_APPLIED = "applied"
# Global secondary index on (object_key, created_at); the same name is set in terraform/main.tf.
OBJECT_HISTORY_INDEX = "object-history"
# Global apply counters share the plan table under keys that can never be a plan id (a UUID).
GLOBAL_BUDGET_PREFIX = "budget#global#"
GLOBAL_BUDGET_WINDOW_SECONDS = 3600
# Approval requests (notifications) across all plans, counted per clock window like the budget.
APPROVAL_REQUEST_PREFIX = "approval#global#"
APPROVAL_REQUEST_WINDOW_SECONDS = 600

# Until per-agent identity reaches the server, every plan is requested by the gateway's
# machine principal class; approvals come only from human IAM principals.
REQUESTER_GATEWAY = "mcp-gateway"
STATUS_PLANNED = "planned"
STATUS_AWAITING = "awaiting_approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
# Reported in approval_required details; never stored.
APPROVAL_EXPIRED = "approval_expired"
APPROVAL_INVALID = "approval_invalid"
CHANNEL_API = "api"


def global_budget_key(now: int) -> str:
    """Counter for the clock hour that contains `now`."""
    return f"{GLOBAL_BUDGET_PREFIX}{now - now % GLOBAL_BUDGET_WINDOW_SECONDS}"


def approval_request_key(now: int) -> str:
    return f"{APPROVAL_REQUEST_PREFIX}{now - now % APPROVAL_REQUEST_WINDOW_SECONDS}"


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def args_hash(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def plan_args_hash(action: str, args: Mapping[str, Any]) -> str:
    """The hash an approval pins: the action and the plan's argument payload (`args`)."""
    return args_hash({"action": action, **args})


def scale_args(
    namespace: str, name: str, replicas: int, override_manual_change: bool = False
) -> dict[str, Any]:
    """The argument payload of a scale plan: the apply arguments without plan_id."""
    args: dict[str, Any] = {"namespace": namespace, "name": name, "replicas": replicas}
    # Only a true override enters the payload, so hashes without it are unchanged.
    if override_manual_change:
        args["override_manual_change"] = True
    return args


def scale_args_hash(
    namespace: str, name: str, replicas: int, override_manual_change: bool = False
) -> str:
    return plan_args_hash(
        SCALE_DEPLOYMENT, scale_args(namespace, name, replicas, override_manual_change)
    )


def restart_args(namespace: str, name: str) -> dict[str, Any]:
    """The argument payload of a restart plan: the apply arguments without plan_id."""
    return {"namespace": namespace, "name": name}


def rollback_args(
    namespace: str, name: str, revision: int, override_manual_change: bool = False
) -> dict[str, Any]:
    """The argument payload of a rollback plan: the apply arguments without plan_id."""
    args: dict[str, Any] = {"namespace": namespace, "name": name, "revision": revision}
    if override_manual_change:
        args["override_manual_change"] = True
    return args


def template_hash(template: Mapping[str, Any]) -> str:
    """Hash of a pod template as the API server returned it (raw JSON, not client models,
    which drop fields the client does not know)."""
    return "sha256:" + hashlib.sha256(_canonical(template)).hexdigest()


def restart_template(template: Mapping[str, Any], plan_id: str) -> dict[str, Any]:
    """The pod template a restart leaves: a copy of `template` with the restart annotation
    set to `plan_id`."""
    restarted = copy.deepcopy(dict(template))
    metadata = restarted.setdefault("metadata", {})
    metadata["annotations"] = {**(metadata.get("annotations") or {}), RESTART_ANNOTATION: plan_id}
    return restarted


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
    requested_by: str = REQUESTER_GATEWAY
    status: str = STATUS_PLANNED
    override_manual_change: bool = False
    # The impact preview returned with the plan, so an approver sees the server's own record.
    impact: Mapping[str, Any] | None = None
    # Written by request_approval and by the approval channel; absent until then.
    approval_requested_at: int | None = None
    approved_by: str | None = None
    approved_at: int | None = None
    approved_args_hash: str | None = None
    approval_channel: str | None = None
    approval_expires_at: int | None = None
    rejected_by: str | None = None
    rejected_at: int | None = None
    reject_reason: str | None = None
    # Correlation ids of the invocations that planned, requested approval, decided and
    # applied (mcp/CONTRACT.md, Observability); absent until each step happens.
    plan_correlation_id: str | None = None
    approval_request_correlation_id: str | None = None
    decision_correlation_id: str | None = None
    apply_correlation_id: str | None = None
    # The argument payload args_hash covers, without the action: the apply arguments
    # without plan_id. Plans made before it was recorded have none and cannot be applied.
    args: Mapping[str, Any] | None = None
    # template_hash of the Deployment's pod template at plan time, and of the template the
    # change leaves; equal for a scale, restart_template's hash for a restart.
    template_from: str | None = None
    template_to: str | None = None
    # The Deployment's revision annotation at plan time, for a change that rolls out a new
    # pod template; information only.
    revision_from: int | None = None
    # Rollback only: the requested revision (the approved argument) and the ReplicaSet it
    # resolved to, which apply re-reads; template_to is the hash of its template.
    revision_to: int | None = None
    target_rs_name: str | None = None
    target_rs_uid: str | None = None
    # Rollback only: the image change per container, as untrusted text, and the names of
    # the other template fields that change (never their values).
    images: Sequence[Mapping[str, Any]] | None = None
    fields_changed: Sequence[str] | None = None
    # Rollback only: whether images or fields_changed was cut, so the approval channel can
    # say that more changes than it shows.
    diff_truncated: bool | None = None

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
            "requested_by": self.requested_by,
            "status": self.status,
            "override_manual_change": self.override_manual_change,
        }
        if self.impact is not None:
            item["impact"] = self.impact
        if self.args is not None:
            item["args"] = dict(self.args)
        if self.images is not None:
            item["images"] = [dict(image) for image in self.images]
        if self.fields_changed is not None:
            item["fields_changed"] = list(self.fields_changed)
        if self.diff_truncated is not None:
            item["diff_truncated"] = self.diff_truncated
        for field in (*_OPTIONAL_INTS, *_OPTIONAL_STRS):
            value = getattr(self, field)
            if value is not None:
                item[field] = value
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
            # Plans made before server approval have neither; they can never be approved.
            requested_by=str(item.get("requested_by", "")),
            status=str(item.get("status", "")),
            override_manual_change=item.get("override_manual_change") is True,
            impact=item.get("impact"),
            args=_args(item.get("args")),
            images=_images(item.get("images")),
            fields_changed=(
                [str(f) for f in item["fields_changed"]]
                if isinstance(item.get("fields_changed"), list)
                else None
            ),
            diff_truncated=(item["diff_truncated"] is True if "diff_truncated" in item else None),
            **{f: int(item[f]) for f in _OPTIONAL_INTS if item.get(f) is not None},
            **{f: str(item[f]) for f in _OPTIONAL_STRS if item.get(f) is not None},
        )


_OPTIONAL_INTS = (
    "retain_until",
    "applied_at",
    "approval_requested_at",
    "approved_at",
    "approval_expires_at",
    "rejected_at",
    "revision_from",
    "revision_to",
)
_OPTIONAL_STRS = (
    "result",
    "approved_by",
    "approved_args_hash",
    "approval_channel",
    "rejected_by",
    "reject_reason",
    "plan_correlation_id",
    "approval_request_correlation_id",
    "decision_correlation_id",
    "apply_correlation_id",
    "template_from",
    "template_to",
    "target_rs_name",
    "target_rs_uid",
)


def _args(value: Any) -> dict[str, Any] | None:
    """The stored argument payload with DynamoDB's Decimal numbers back as integers."""
    if not isinstance(value, Mapping):
        return None
    return {str(k): int(v) if isinstance(v, Decimal) else v for k, v in value.items()}


def _images(value: Any) -> list[dict[str, Any]] | None:
    """The stored image change: strings, booleans and nulls only, so nothing to convert."""
    if not isinstance(value, list):
        return None
    return [{str(k): v for k, v in image.items()} for image in value if isinstance(image, Mapping)]


def _with_correlation(
    update: str, values: dict[str, Any], attribute: str, correlation_id: str | None
) -> tuple[str, dict[str, Any]]:
    """Add `SET attribute = id` to an update expression when there is an id."""
    if correlation_id is None:
        return update, values
    return f"{update}, {attribute} = :cid", {**values, ":cid": correlation_id}


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

    def consume(
        self,
        plan_id: str,
        expected_hash: str,
        action: str,
        now: int,
        require_approval: bool = False,
    ) -> Plan:
        """Atomically mark the plan used; raise GuardError with the precise reason otherwise.

        With `require_approval`, the same conditional write also requires a server approval
        of exactly these arguments, by someone other than the requester, that has not expired.
        """
        # A rejection also ends the plan's validity; this holds even if clocks disagree.
        condition = (
            "#used = :false AND expires_at > :now AND args_hash = :h AND #action = :a"
            " AND attribute_not_exists(rejected_by)"
        )
        names = {"#used": "used", "#action": "action"}
        values: dict[str, Any] = {
            ":true": True,
            ":false": False,
            ":now": now,
            ":h": expected_hash,
            ":a": action,
        }
        if require_approval:
            condition += (
                " AND #status = :approved AND attribute_exists(approved_by)"
                " AND approved_by <> requested_by AND approved_args_hash = args_hash"
                " AND approval_expires_at > :now"
            )
            names["#status"] = "status"
            values[":approved"] = STATUS_APPROVED
        try:
            response = self._table.update_item(
                Key={"plan_id": plan_id},
                UpdateExpression="SET #used = :true",
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise
            raise self._rejection(plan_id, expected_hash, action, now, require_approval) from None
        return Plan.from_item(response["Attributes"])

    def get(self, plan_id: str) -> dict[str, Any] | None:
        """The raw record, read consistently."""
        return self._table.get_item(Key={"plan_id": plan_id}, ConsistentRead=True).get("Item")

    def request_approval(
        self,
        plan_id: str,
        now: int,
        wait_ttl: int,
        cooldown: int,
        limit: int,
        correlation_id: str | None = None,
    ) -> tuple[dict[str, Any], int | None]:
        """Move a plan to awaiting_approval and give it `wait_ttl` seconds from its creation.

        Returns the record and None when approvers should be notified now. When this plan
        already asked less than `cooldown` seconds ago, nothing changes and the second value
        is the time it may ask again. A notification takes one unit of the global limit first.
        """
        current = self.get(plan_id)
        retry_after = _request_state(current, now, cooldown)
        if current is not None and retry_after is not None:
            return current, retry_after
        window_start = now - now % APPROVAL_REQUEST_WINDOW_SECONDS
        self._take_counter(
            approval_request_key(now),
            "requests",
            limit,
            window_start + 2 * APPROVAL_REQUEST_WINDOW_SECONDS,
            approval_rate_error(window_start, limit),
        )
        update, values = _with_correlation(
            "SET #status = :awaiting, approval_requested_at = :now, "
            "expires_at = created_at + :wait",
            {
                ":false": False,
                ":now": now,
                ":wait": wait_ttl,
                ":cutoff": now - cooldown,
                ":planned": STATUS_PLANNED,
                ":awaiting": STATUS_AWAITING,
            },
            "approval_request_correlation_id",
            correlation_id,
        )
        try:
            response = self._table.update_item(
                Key={"plan_id": plan_id},
                UpdateExpression=update,
                ConditionExpression=(
                    "#used = :false AND expires_at > :now AND #status IN (:planned, :awaiting)"
                    " AND (attribute_not_exists(approval_requested_at)"
                    " OR approval_requested_at <= :cutoff)"
                ),
                ExpressionAttributeNames={"#used": "used", "#status": "status"},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise
            item = self.get(plan_id)
            retry_after = _request_state(item, now, cooldown)
            if retry_after is None:
                raise GuardError(CONFLICT, "plan changed concurrently; try again") from None
            return item or {}, retry_after
        return response["Attributes"], None

    def approve(
        self,
        plan_id: str,
        approver: str,
        now: int,
        apply_ttl: int,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a human approval pinned to the plan's own args_hash, in one conditional write.

        The plan then stays valid for `apply_ttl` seconds, whatever was left of it before.
        """
        expires = now + apply_ttl
        return self._decide(
            plan_id,
            approver,
            now,
            *_with_correlation(
                "SET #status = :decided, approved_by = :by, approved_at = :now, "
                "approved_args_hash = args_hash, approval_channel = :channel, "
                "approval_expires_at = :expires, expires_at = :expires",
                {":decided": STATUS_APPROVED, ":channel": CHANNEL_API, ":expires": expires},
                "decision_correlation_id",
                correlation_id,
            ),
        )

    def reject(
        self,
        plan_id: str,
        approver: str,
        now: int,
        reason: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a rejection. The plan expires at once, so no mode can apply it any more."""
        return self._decide(
            plan_id,
            approver,
            now,
            *_with_correlation(
                "SET #status = :decided, rejected_by = :by, rejected_at = :now, "
                "reject_reason = :reason, expires_at = :now",
                {":decided": STATUS_REJECTED, ":reason": reason},
                "decision_correlation_id",
                correlation_id,
            ),
        )

    def _decide(
        self, plan_id: str, approver: str, now: int, update: str, values: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            response = self._table.update_item(
                Key={"plan_id": plan_id},
                UpdateExpression=update,
                ConditionExpression=(
                    "#status IN (:planned, :awaiting) AND #used = :false AND expires_at > :now"
                    " AND requested_by <> :by"
                ),
                ExpressionAttributeNames={"#status": "status", "#used": "used"},
                ExpressionAttributeValues={
                    ":planned": STATUS_PLANNED,
                    ":awaiting": STATUS_AWAITING,
                    ":false": False,
                    ":now": now,
                    ":by": approver,
                    **values,
                },
                ReturnValues="ALL_NEW",
            )
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise
            raise _decision_error(self.get(plan_id), now, approver) from None
        return response["Attributes"]

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

    def record_result(
        self, plan_id: str, applied_at: int, result: str, correlation_id: str | None = None
    ) -> None:
        update, values = _with_correlation(
            "SET applied_at = :t, #result = :r",
            {":t": applied_at, ":r": result},
            "apply_correlation_id",
            correlation_id,
        )
        self._table.update_item(
            Key={"plan_id": plan_id},
            UpdateExpression=update,
            ExpressionAttributeNames={"#result": "result"},
            ExpressionAttributeValues=values,
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
        self._take_counter(
            global_budget_key(now),
            "applies",
            limit,
            start + 2 * GLOBAL_BUDGET_WINDOW_SECONDS,
            global_budget_error(start, limit),
        )

    def _take_counter(
        self, key: str, attribute: str, limit: int, retain_until: int, error: GuardError
    ) -> None:
        """Add 1 to a counter item only while it is below `limit`; raise `error` otherwise."""
        try:
            self._table.update_item(
                Key={"plan_id": key},
                UpdateExpression="ADD #count :one SET retain_until = :retain",
                ConditionExpression="attribute_not_exists(#count) OR #count < :limit",
                ExpressionAttributeNames={"#count": attribute},
                ExpressionAttributeValues={":one": 1, ":limit": limit, ":retain": retain_until},
            )
        except ClientError as exc:
            if not _is_conditional_failure(exc):
                raise
            raise error from None

    def _rejection(
        self, plan_id: str, expected_hash: str, action: str, now: int, require_approval: bool
    ) -> GuardError:
        item = self.get(plan_id)
        if item is None:
            return GuardError(PLAN_NOT_FOUND, "plan not found")
        if item.get("used") is True:
            return GuardError(PLAN_USED, "plan already used")
        status = item.get("status")
        if status == STATUS_REJECTED:
            return _approval_required(STATUS_REJECTED, "the plan was rejected by an approver")
        if (
            require_approval
            and status == STATUS_APPROVED
            and Decimal(item.get("approval_expires_at", 0)) <= now
        ):
            return _approval_required(
                APPROVAL_EXPIRED, "the approval expired before the apply; plan again"
            )
        if Decimal(item.get("expires_at", 0)) <= now:
            return GuardError(PLAN_EXPIRED, "plan expired")
        if item.get("action") != action or item.get("args_hash") != expected_hash:
            return GuardError(PLAN_MISMATCH, "arguments do not match the plan")
        if require_approval:
            if status != STATUS_APPROVED or "approved_by" not in item:
                return _approval_required(
                    str(status or STATUS_PLANNED),
                    "the plan has no server approval; call request_approval and wait for an "
                    "approver",
                )
            if item.get("approved_by") == item.get("requested_by") or item.get(
                "approved_args_hash"
            ) != item.get("args_hash"):
                return _approval_required(APPROVAL_INVALID, "the approval does not cover this plan")
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


def approval_rate_error(window_start: int, limit: int) -> GuardError:
    return GuardError(
        APPROVAL_RATE_LIMITED,
        f"the limit of {limit} approval requests in {APPROVAL_REQUEST_WINDOW_SECONDS} s has "
        "been reached",
        {
            "limit": limit,
            "window_seconds": APPROVAL_REQUEST_WINDOW_SECONDS,
            "window_start": window_start,
            "retry_after": window_start + APPROVAL_REQUEST_WINDOW_SECONDS,
        },
    )


def _approval_required(status: str, message: str) -> GuardError:
    return GuardError(APPROVAL_REQUIRED, message, {"status": status})


def _decided(item: Mapping[str, Any]) -> GuardError | None:
    status = item.get("status")
    if status in (STATUS_APPROVED, STATUS_REJECTED):
        return GuardError(PLAN_DECIDED, f"the plan was already {status}", {"status": status})
    return None


def _request_state(item: Mapping[str, Any] | None, now: int, cooldown: int) -> int | None:
    """Raise if the plan cannot await approval; return when it may notify again if it asked
    within the cooldown, otherwise None."""
    if item is None:
        raise GuardError(PLAN_NOT_FOUND, "plan not found")
    if item.get("used") is True:
        raise GuardError(PLAN_USED, "plan already used")
    decided = _decided(item)
    if decided is not None:
        raise decided
    if Decimal(item.get("expires_at", 0)) <= now:
        raise GuardError(PLAN_EXPIRED, "plan expired")
    if item.get("status") not in (STATUS_PLANNED, STATUS_AWAITING):
        raise GuardError(PLAN_EXPIRED, "plan predates server approval; plan again")
    requested = item.get("approval_requested_at")
    if requested is not None and int(requested) > now - cooldown:
        return int(requested) + cooldown
    return None


def _decision_error(item: Mapping[str, Any] | None, now: int, approver: str) -> GuardError:
    if item is None:
        return GuardError(PLAN_NOT_FOUND, "plan not found")
    if item.get("used") is True:
        return GuardError(PLAN_USED, "plan already used")
    decided = _decided(item)
    if decided is not None:
        return decided
    if Decimal(item.get("expires_at", 0)) <= now:
        return GuardError(PLAN_EXPIRED, "plan expired")
    if item.get("status") not in (STATUS_PLANNED, STATUS_AWAITING):
        return GuardError(PLAN_EXPIRED, "plan predates server approval; plan again")
    if item.get("requested_by") == approver:
        return GuardError(FORBIDDEN, "the requester of a plan cannot decide on it")
    return GuardError(CONFLICT, "plan changed concurrently; try again")
