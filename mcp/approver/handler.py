"""Approver Lambda: the human approval channel (mcp/CONTRACT.md, Server-side approval).

Served through a Lambda Function URL with AWS_IAM auth, never through the gateway, so no MCP
client can reach it. The caller is the IAM principal that signed the request; only listed
human principals may decide, and machine identities are refused even when listed.
"""

import base64
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

from handrail_common.approval import plain, plan_view, sanitize_reason
from handrail_common.config import SERVER, ConfigError, approval_mode, int_env, required_env
from handrail_common.envelope import (
    CONFLICT,
    FORBIDDEN,
    INTERNAL,
    INVALID_ARGUMENT,
    PLAN_DECIDED,
    PLAN_EXPIRED,
    PLAN_NOT_FOUND,
    PLAN_USED,
    Envelope,
    err,
    ok,
)
from handrail_common.observability import APPROVER_CORRELATION_KEY, emit
from handrail_common.plans import SCALE_DEPLOYMENT, PlanStore
from handrail_common.validation import GuardError, require_plan_id

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# The write tool that applies each plan action, as the gateway names it.
APPLY_TOOLS = {SCALE_DEPLOYMENT: "k8sw___apply_scale_deployment"}
APPROVE = "approve"
REJECT = "reject"
BODY_FIELDS = frozenset({"plan_id", "decision", "reason", "apply"})
MAX_BODY_BYTES = 4096

HTTP_STATUS = {
    INVALID_ARGUMENT: 400,
    FORBIDDEN: 403,
    PLAN_NOT_FOUND: 404,
    PLAN_USED: 409,
    PLAN_EXPIRED: 409,
    PLAN_DECIDED: 409,
    CONFLICT: 409,
}

# IAM user: arn:<p>:iam::<account>:user[/path]/<name>
# IAM role: arn:<p>:iam::<account>:role[/path]/<name>, whose sessions sign as
#           arn:<p>:sts::<account>:assumed-role/<name>/<session> (the path is dropped).
_IAM_ARN = re.compile(r"^arn:(aws[a-z-]*):iam::(\d{12}):(user|role)/(?:[^/]+/)*([^/]+)$")
_SESSION_ARN = re.compile(r"^arn:(aws[a-z-]*):sts::(\d{12}):assumed-role/([^/]+)/[^/]+$")


@dataclass(frozen=True)
class ApproverSettings:
    plans_table: str
    write_function: str
    approvers: tuple[str, ...]
    # The Lambdas' own roles: machine identities that can never approve, listed or not.
    denied: tuple[str, ...]
    approval_mode: str
    apply_after_approval_ttl_seconds: int
    region: str | None

    @property
    def applies_after_approval(self) -> bool:
        """Only in server mode. In both mode the client's own approval must still happen, so
        the client applies; in client mode the server approval is not required at all."""
        return self.approval_mode == SERVER


def _arns(env: Mapping[str, str], key: str) -> tuple[str, ...]:
    values = tuple(v.strip() for v in env.get(key, "").split(",") if v.strip())
    for value in values:
        if not _IAM_ARN.match(value):
            raise ConfigError(f"{key}: {value!r} is not an IAM user or role ARN")
    return values


def load_approver_settings(env: Mapping[str, str] | None = None) -> ApproverSettings:
    env = os.environ if env is None else env
    ttl = int_env(env, "APPLY_AFTER_APPROVAL_TTL_SECONDS", 120)
    if ttl <= 0:
        raise ConfigError("APPLY_AFTER_APPROVAL_TTL_SECONDS must be positive")
    return ApproverSettings(
        plans_table=required_env(env, "PLANS_TABLE"),
        write_function=required_env(env, "WRITE_FUNCTION_NAME"),
        approvers=_arns(env, "APPROVER_PRINCIPALS"),
        denied=_arns(env, "DENIED_PRINCIPALS"),
        approval_mode=approval_mode(env),
        apply_after_approval_ttl_seconds=ttl,
        region=env.get("AWS_REGION") or None,
    )


def principal_matches(entry: str, caller: str) -> bool:
    """An IAM user ARN matches itself; an IAM role ARN matches its assumed-role sessions."""
    listed = _IAM_ARN.match(entry)
    if listed is None:
        return False
    partition, account, kind, name = listed.groups()
    if kind == "user":
        return caller == entry
    session = _SESSION_ARN.match(caller)
    return session is not None and session.groups() == (partition, account, name)


def check_approver(settings: ApproverSettings, caller: str | None) -> str:
    if not caller:
        raise GuardError(FORBIDDEN, "the request carries no IAM identity")
    if any(principal_matches(entry, caller) for entry in settings.denied):
        raise GuardError(FORBIDDEN, "machine identities cannot approve")
    if not any(principal_matches(entry, caller) for entry in settings.approvers):
        raise GuardError(FORBIDDEN, "the caller is not an approver")
    return caller


def caller_arn(event: Mapping[str, Any]) -> str | None:
    """The principal that signed the request, as the Function URL's IAM auth reports it."""
    iam = ((event.get("requestContext") or {}).get("authorizer") or {}).get("iam") or {}
    arn = iam.get("userArn")
    return arn if isinstance(arn, str) and arn else None


def _body(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or ""
    if not isinstance(raw, str):
        raise GuardError(INVALID_ARGUMENT, "body must be a JSON object")
    try:
        text = base64.b64decode(raw).decode("utf-8") if event.get("isBase64Encoded") else raw
    except (ValueError, UnicodeDecodeError):
        raise GuardError(INVALID_ARGUMENT, "body must be UTF-8 JSON") from None
    if len(text.encode("utf-8")) > MAX_BODY_BYTES:
        raise GuardError(INVALID_ARGUMENT, f"body must be at most {MAX_BODY_BYTES} bytes")
    try:
        body = json.loads(text)
    except ValueError:
        raise GuardError(INVALID_ARGUMENT, "body must be a JSON object") from None
    if not isinstance(body, dict):
        raise GuardError(INVALID_ARGUMENT, "body must be a JSON object")
    unknown = sorted(set(body) - BODY_FIELDS)
    if unknown:
        raise GuardError(INVALID_ARGUMENT, f"unknown fields: {', '.join(unknown)}")
    return body


@dataclass
class Runtime:
    settings: ApproverSettings
    store: PlanStore
    lambda_client: Any
    clock: Callable[[], float] = time.time


def check_appliable(item: Mapping[str, Any]) -> None:
    """Refuse, before any decision is recorded, a plan that no apply tool could run."""
    if item.get("action") not in APPLY_TOOLS:
        raise GuardError(INTERNAL, "no apply tool for this plan's action")
    if not isinstance(item.get("args"), Mapping):
        raise GuardError(PLAN_EXPIRED, "plan predates the argument record; plan again")


def apply_arguments(item: Mapping[str, Any]) -> dict[str, Any]:
    """The plan's own argument payload from the record, unchanged, plus its plan_id; the
    write guard re-checks all of it."""
    return {"plan_id": item["plan_id"], **plain(item["args"])}


def trigger_apply(
    rt: Runtime, item: Mapping[str, Any], correlation_id: str | None = None
) -> Envelope:
    """Run the apply through the write Lambda, as the gateway would call it. The apply
    takes this request's correlation id, so the decision and the apply share one."""
    custom = {"bedrockAgentCoreToolName": APPLY_TOOLS[item["action"]]}
    if correlation_id is not None:
        custom[APPROVER_CORRELATION_KEY] = correlation_id
    context = {"custom": custom}
    try:
        response = rt.lambda_client.invoke(
            FunctionName=rt.settings.write_function,
            InvocationType="RequestResponse",
            ClientContext=base64.b64encode(json.dumps(context).encode()).decode(),
            Payload=json.dumps(apply_arguments(item)).encode(),
        )
    except Exception as exc:
        # The decision is already recorded; report that the apply did not start.
        reason = (
            exc.response.get("Error", {}).get("Code", "unknown")
            if isinstance(exc, ClientError)
            else type(exc).__name__
        )
        logger.exception("cannot invoke the apply of plan %s", item["plan_id"])
        return err(INTERNAL, f"approval recorded; apply could not be started: {reason}")
    if response.get("FunctionError"):
        logger.error("apply of plan %s failed: %s", item["plan_id"], response["FunctionError"])
        return err(INTERNAL, "the apply failed to run")
    try:
        envelope = json.loads(response["Payload"].read())
    except (ValueError, KeyError):
        return err(INTERNAL, "the apply returned an unreadable response")
    return envelope if isinstance(envelope, dict) else err(INTERNAL, "unexpected apply response")


def show(rt: Runtime, event: Mapping[str, Any]) -> Envelope:
    params = event.get("queryStringParameters") or {}
    plan_id = require_plan_id(params.get("plan_id"))
    item = rt.store.get(plan_id)
    if item is None:
        raise GuardError(PLAN_NOT_FOUND, "plan not found")
    return ok(plan_view(item, int(rt.clock())))


def decide(
    rt: Runtime, event: Mapping[str, Any], approver: str, correlation_id: str | None = None
) -> Envelope:
    body = _body(event)
    plan_id = require_plan_id(body.get("plan_id"))
    decision = body.get("decision")
    if decision not in (APPROVE, REJECT):
        raise GuardError(INVALID_ARGUMENT, "decision must be approve or reject")
    apply = body.get("apply", True)
    if not isinstance(apply, bool):
        raise GuardError(INVALID_ARGUMENT, "apply must be a boolean")
    reason = sanitize_reason(body.get("reason"))

    now = int(rt.clock())
    s = rt.settings
    if decision == APPROVE:
        current = rt.store.get(plan_id)
        # A missing plan is left to the approval itself, which says why it cannot be approved.
        if current is not None:
            check_appliable(current)
        item = rt.store.approve(
            plan_id, approver, now, s.apply_after_approval_ttl_seconds, correlation_id
        )
    else:
        item = rt.store.reject(plan_id, approver, now, reason, correlation_id)

    applied: Envelope | None = None
    if decision == APPROVE and apply and s.applies_after_approval:
        applied = trigger_apply(rt, item, correlation_id)
        refreshed = rt.store.get(plan_id)
        item = refreshed if refreshed is not None else item
    emit(decision_record(plan_id, decision, approver, correlation_id, applied))
    return ok(
        {
            "plan": plan_view(item, int(rt.clock())),
            "apply": applied,
            "approval_mode": s.approval_mode,
        }
    )


def decision_record(
    plan_id: str,
    decision: str,
    approver: str,
    correlation_id: str | None,
    applied: Envelope | None,
) -> dict[str, Any]:
    """One JSON log line per decision. The reason is free text and is not logged."""
    apply_outcome = None
    if applied is not None:
        apply_outcome = "ok" if applied.get("ok") else (applied.get("error") or {}).get("code")
    return {
        "event": "decision",
        "plan_id": plan_id,
        "decision": decision,
        "approver": approver,
        "correlation_id": correlation_id,
        "apply_outcome": apply_outcome,
    }


def handle(rt: Runtime, event: Mapping[str, Any], correlation_id: str | None = None) -> Envelope:
    approver = check_approver(rt.settings, caller_arn(event))
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method")
    if method == "GET":
        return show(rt, event)
    if method == "POST":
        return decide(rt, event, approver, correlation_id)
    raise GuardError(INVALID_ARGUMENT, "use GET to show a plan or POST to decide")


def response(envelope: Envelope) -> dict[str, Any]:
    code = (envelope.get("error") or {}).get("code")
    status = 200 if envelope.get("ok") else HTTP_STATUS.get(code, 500)
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(envelope),
    }


def run(rt: Runtime, event: Any, correlation_id: str | None = None) -> dict[str, Any]:
    event = event if isinstance(event, Mapping) else {}
    try:
        envelope = handle(rt, event, correlation_id)
    except GuardError as exc:
        if exc.code == FORBIDDEN:
            logger.warning("refused %s: %s", caller_arn(event), exc.message)
        envelope = err(exc.code, exc.message, exc.details)
    except Exception:
        logger.exception("approval request failed")
        envelope = err(INTERNAL, "internal error")
    return response(envelope)


_runtime: Runtime | None = None


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    global _runtime
    if _runtime is None:
        settings = load_approver_settings()
        _runtime = Runtime(
            settings=settings,
            store=PlanStore.from_name(settings.plans_table, settings.region),
            lambda_client=boto3.client("lambda", region_name=settings.region),
        )
    # The approver is not behind the gateway: its correlation id is its own request id.
    return run(_runtime, event, getattr(context, "aws_request_id", None))
