"""What an approver sees: a plan rendered from the server's own record (mcp/CONTRACT.md,
Server-side approval). Nothing here comes from text an agent supplied.
"""

import unicodedata
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from handrail_common.envelope import INVALID_ARGUMENT
from handrail_common.plans import (
    RESTART_ANNOTATION,
    RESTART_DEPLOYMENT,
    ROLLBACK_DEPLOYMENT,
    STATUS_PLANNED,
    STATUS_REJECTED,
)
from handrail_common.validation import GuardError

STATUS_APPLIED = "applied"
STATUS_EXPIRED = "expired"
REASON_MAX_LENGTH = 500
APPROVE_SCRIPT = "scripts/approve.py"
# Plan record attributes <step>_correlation_id, shown so an operator can find each step's logs.
CORRELATION_STEPS = ("plan", "approval_request", "decision", "apply")


def plain(value: Any) -> Any:
    """DynamoDB returns numbers as Decimal; every number in a plan record is an integer."""
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return value


def derived_status(item: Mapping[str, Any], now: int) -> str:
    """The lifecycle state: applied and expired are derived from `used` and `expires_at`."""
    if item.get("used") is True:
        return STATUS_APPLIED
    status = str(item.get("status") or STATUS_PLANNED)
    if status == STATUS_REJECTED:
        return status
    if int(item.get("expires_at", 0)) <= now:
        return STATUS_EXPIRED
    return status


def change_view(record: Mapping[str, Any]) -> dict[str, Any]:
    """What the plan changes, per action, in the shape of the plan response's `diff`."""
    if record.get("action") == RESTART_DEPLOYMENT:
        return {
            "restart": {"pods_replaced": record.get("replicas_from")},
            "template": {"from": record.get("template_from"), "to": record.get("template_to")},
        }
    if record.get("action") == ROLLBACK_DEPLOYMENT:
        return {
            "revision": {"from": record.get("revision_from"), "to": record.get("revision_to")},
            "template": {"from": record.get("template_from"), "to": record.get("template_to")},
            "replica_set": record.get("target_rs_name"),
            "images": record.get("images") or [],
            "fields_changed": record.get("fields_changed") or [],
        }
    return {"replicas": {"from": record.get("replicas_from"), "to": record.get("replicas_to")}}


def plan_view(item: Mapping[str, Any], now: int) -> dict[str, Any]:
    record = plain(item)
    view: dict[str, Any] = {
        "plan_id": record["plan_id"],
        "status": derived_status(record, now),
        "action": record.get("action"),
        "namespace": record.get("namespace"),
        "name": record.get("name"),
        "replicas": {"from": record.get("replicas_from"), "to": record.get("replicas_to")},
        "change": change_view(record),
        "override_manual_change": record.get("override_manual_change") is True,
        "impact": record.get("impact"),
        "requested_by": record.get("requested_by"),
        "created_at": record.get("created_at"),
        "expires_at": record.get("expires_at"),
    }
    if "approved_by" in record:
        view["approval"] = {
            key: record.get(key)
            for key in ("approved_by", "approved_at", "approval_channel", "approval_expires_at")
        }
    if "rejected_by" in record:
        view["rejection"] = {
            "rejected_by": record.get("rejected_by"),
            "rejected_at": record.get("rejected_at"),
            "reason": record.get("reject_reason"),
        }
    if "result" in record:
        view["result"] = {"result": record.get("result"), "applied_at": record.get("applied_at")}
    correlation = {
        step: record[f"{step}_correlation_id"]
        for step in CORRELATION_STEPS
        if f"{step}_correlation_id" in record
    }
    if correlation:
        view["correlation_ids"] = correlation
    return view


def _impact_lines(impact: Mapping[str, Any] | None) -> list[str]:
    if not impact:
        return ["Impact: not recorded"]
    pods = impact.get("pods") or {}
    rollout = impact.get("rollout")
    if rollout is None:
        lines = [
            f"Pods: {pods.get('ready_now')} ready now, {pods.get('added')} added, "
            f"{pods.get('removed')} removed"
        ]
    else:
        lines = [f"Pods: {pods.get('ready_now')} ready now, {pods.get('replaced')} replaced"]
        if rollout.get("max_unavailable") is None:
            lines.append(f"Rollout: {rollout.get('strategy')}, every pod stops first")
        else:
            lines.append(
                f"Rollout: {rollout.get('strategy')}, up to {rollout.get('max_unavailable')} "
                f"unavailable, {rollout.get('max_surge')} surge"
            )
    for pdb in impact.get("pod_disruption_budgets") or []:
        lines.append(
            f"PodDisruptionBudget {pdb.get('name')}: disruptions allowed now "
            f"{pdb.get('disruptions_allowed')}"
        )
    for service in impact.get("services") or []:
        if "projected_min_ready_endpoints" in service:
            projected = f"{service.get('projected_min_ready_endpoints')} projected at the lowest"
        else:
            projected = f"{service.get('projected_ready_endpoints')} projected"
        lines.append(
            f"Service {service.get('name')}: {service.get('ready_endpoints_now')} ready "
            f"endpoints now, {projected}"
        )
    warnings = impact.get("warnings") or []
    lines.append("Warnings:" if warnings else "Warnings: none")
    lines += [f"- {w.get('code')}: {w.get('message')}" for w in warnings]
    if impact.get("truncated"):
        lines.append("(lists truncated)")
    return lines


def _rollback_lines(change: Mapping[str, Any]) -> list[str]:
    """The revision, each image change and the other changed fields. Images are shown as
    the record stores them, wrapped as untrusted text: they come from the cluster."""
    revision = change.get("revision") or {}
    lines = [
        f"Rollback: revision {revision.get('from')} -> {revision.get('to')} "
        f"(ReplicaSet {change.get('replica_set')}); replicas stay as they are"
    ]
    for image in change.get("images") or []:
        if image.get("changed"):
            kind = "init container" if image.get("init") else "container"
            lines.append(
                f"Image of {kind} {image.get('container')}: {image.get('from')} -> {image.get('to')}"
            )
    fields = [f for f in change.get("fields_changed") or [] if not f.endswith("].image")]
    lines.append(f"Other changed fields: {', '.join(fields) if fields else 'none'}")
    return lines


def notification(view: Mapping[str, Any], approval_url: str | None) -> str:
    """The approval request sent to approvers, built from plan_view only."""
    plan_id = view["plan_id"]
    url = approval_url or "<approval_url output>"
    replicas = view["replicas"]
    if view.get("action") == RESTART_DEPLOYMENT:
        change = [
            f"Restart: every pod replaced, replicas stay at {replicas['from']}; "
            f"pods get the annotation {RESTART_ANNOTATION}={plan_id}"
        ]
    elif view.get("action") == ROLLBACK_DEPLOYMENT:
        change = _rollback_lines(view.get("change") or {})
    else:
        change = [f"Replicas: {replicas['from']} -> {replicas['to']}"]
    return "\n".join(
        [
            "kube-handrail: a plan is waiting for approval.",
            "",
            f"Plan: {plan_id}",
            f"Deployment: {view['namespace']}/{view['name']}",
            *change,
            f"Override manual change: {'yes' if view['override_manual_change'] else 'no'}",
            f"Requested by: {view['requested_by']}",
            f"Expires at (epoch seconds): {view['expires_at']}",
            "",
            *_impact_lines(view.get("impact")),
            "",
            "Review and decide with your own AWS credentials:",
            f"  APPROVAL_URL={url} {APPROVE_SCRIPT} show {plan_id}",
            f"  APPROVAL_URL={url} {APPROVE_SCRIPT} approve {plan_id}",
            f'  APPROVAL_URL={url} {APPROVE_SCRIPT} reject {plan_id} --reason "..."',
        ]
    )


def sanitize_reason(value: Any) -> str:
    """An optional free-text reason: control characters removed, length bounded."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise GuardError(INVALID_ARGUMENT, "reason must be a string")
    cleaned = "".join(
        " " if ch in "\t\n\r" else ch
        for ch in value
        if ch in "\t\n\r" or not unicodedata.category(ch).startswith("C")
    ).strip()
    if len(cleaned) > REASON_MAX_LENGTH:
        raise GuardError(INVALID_ARGUMENT, f"reason must be at most {REASON_MAX_LENGTH} characters")
    return cleaned
