"""k8s_write Lambda: apply a previously planned Deployment change."""

import json
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import boto3
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

from handrail_common import verify
from handrail_common.agents import check_plan_agent, guard_apply, take_agent_budget
from handrail_common.breaker import (
    LAST_PLAN_ANNOTATION,
    check_history,
    check_manual_change,
    check_template_manual_change,
    last_applied_plan,
    last_plan_id,
    replica_baseline,
    retention_seconds,
)
from handrail_common.config import ConfigError, Settings, load_settings
from handrail_common.dispatch import dispatch
from handrail_common.eks import api_client
from handrail_common.envelope import (
    CONFLICT,
    GITOPS_MANAGED,
    INTERNAL,
    KUBERNETES_ERROR,
    NOT_FOUND,
    OUT_OF_BOUNDS,
    PLAN_EXPIRED,
    PLAN_MISMATCH,
    REMEDIATION_DISABLED,
    REVISION_NOT_FOUND,
    Envelope,
    ok,
)
from handrail_common.gitops import is_gitops_managed
from handrail_common.killswitch import KillSwitch
from handrail_common.observability import (
    CHANNEL_VERIFIER,
    CORRELATION_ANNOTATION,
    current_channel,
    current_correlation_id,
)
from handrail_common.plans import (
    KIND_REVERT,
    REQUESTER_REVERT,
    RESTART_ANNOTATION,
    RESTART_DEPLOYMENT,
    RESULT_APPLIED,
    ROLLBACK_DEPLOYMENT,
    SCALE_DEPLOYMENT,
    STATUS_REVERT,
    Plan,
    PlanStore,
    plan_args_hash,
    restart_args,
    revert_plan_id,
    rollback_args,
    scale_args,
    template_hash,
)
from handrail_common.rollout import is_controlled_by, require_revision, template_of
from handrail_common.validation import (
    GuardError,
    check_not_paused,
    check_replica_step,
    require_override_flag,
    require_plan_id,
    require_verify_args,
    validate_scale_args,
    validate_target_args,
)

logger = logging.getLogger(__name__)

DEFAULT_REPLICAS = 1
# Status updates (for example during a rollout) change resourceVersion but not the spec.
# A 409 caused by one is retried after re-checking the object; a spec change never is.
PATCH_ATTEMPTS = 3
# Set by every apply of a plan that names its agent, so humans and admission see who asked.
REQUESTED_BY_ANNOTATION = "kube-handrail.io/requested-by"


@dataclass
class Runtime:
    settings: Settings
    kill_switch: KillSwitch
    store: PlanStore
    apps: Callable[[], k8s.AppsV1Api]
    clock: Callable[[], float] = time.time
    # For the per-agent kill switches; the global one has its own client.
    ssm: Any = None


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    """Build dependencies once per execution environment."""
    global _runtime
    if _runtime is None:
        settings = load_settings(write=True)
        param = settings.remediation_enabled_param
        if param is None:
            raise ConfigError("REMEDIATION_ENABLED_PARAM is required")
        ssm = boto3.client("ssm", region_name=settings.region)
        _runtime = Runtime(
            settings=settings,
            kill_switch=KillSwitch(ssm, param),
            store=PlanStore.from_name(settings.plans_table, settings.region),
            apps=lambda: k8s.AppsV1Api(api_client(settings.cluster_name)),
            ssm=ssm,
        )
    return _runtime


def _no_checks(*_args: Any) -> None:
    pass


@dataclass(frozen=True)
class Action:
    """What one action adds to the generic apply path (_apply)."""

    # The plan record's `action`.
    name: str
    # Argument checks 1-3: (namespace, name, args), where args is the argument payload the
    # plan stored: the apply arguments without plan_id, optional ones only when given.
    validate: Callable[[Mapping[str, Any], Settings], tuple[str, str, dict[str, Any]]]
    # Whether the record's own fields describe these arguments (besides namespace and name).
    record_matches: Callable[[Plan, Mapping[str, Any]], bool]
    # After the consume, before the Deployment is read.
    check_plan: Callable[[Runtime, Plan], None]
    # History checks (manual change, cooldown, flapping, object budget); the generic path
    # takes the global budget unit after them.
    breaker: Callable[[Runtime, Plan, Any, bool, int], None]
    # The patch body for the Deployment just read and checked, and what extra_checks
    # returned for it.
    body: Callable[[Plan, Mapping[str, Any], Any, Any, str | None], Any]
    # Action-specific fields of the response data.
    result: Callable[[Plan, Mapping[str, Any]], dict[str, Any]]
    # Further checks on the live Deployment, after the generic ones, before every attempt;
    # returns what the body needs besides the Deployment (rollback: the template it checked).
    extra_checks: Callable[[k8s.AppsV1Api, Plan, Any], Any] = _no_checks


def _annotations(
    plan_id: str, correlation_id: str | None, requested_by: str | None
) -> dict[str, str]:
    annotations = {LAST_PLAN_ANNOTATION: plan_id}
    if correlation_id is not None:
        annotations[CORRELATION_ANNOTATION] = correlation_id
    if requested_by is not None:
        annotations[REQUESTED_BY_ANNOTATION] = requested_by
    return annotations


def _patch_metadata(
    resource_version: str, plan_id: str, correlation_id: str | None, requested_by: str | None
) -> dict[str, Any]:
    # resourceVersion in the patch body makes the API server reject it with 409 if the
    # object changed between the spec check and the patch.
    return {
        "resourceVersion": resource_version,
        "annotations": _annotations(plan_id, correlation_id, requested_by),
    }


def agent_requester(plan: Plan) -> str | None:
    """The requested-by annotation's value: only for a plan that names its agent."""
    return plan.requested_by if plan.agent is not None else None


def scale_patch(
    replicas: int,
    resource_version: str,
    plan_id: str,
    correlation_id: str | None = None,
    requested_by: str | None = None,
) -> dict[str, Any]:
    return {
        "metadata": _patch_metadata(resource_version, plan_id, correlation_id, requested_by),
        "spec": {"replicas": replicas},
    }


def restart_patch(
    resource_version: str,
    plan_id: str,
    correlation_id: str | None = None,
    requested_by: str | None = None,
) -> dict[str, Any]:
    """Only the restart annotation changes in the pod template, so the template written is
    the one the plan pinned (template_to)."""
    return {
        "metadata": _patch_metadata(resource_version, plan_id, correlation_id, requested_by),
        "spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: plan_id}}}},
    }


def _pointer(key: str) -> str:
    """An annotation key as a JSON Pointer token (RFC 6901): `~` and `/` escaped."""
    return key.replace("~", "~0").replace("/", "~1")


def rollback_patch(
    template: Mapping[str, Any],
    resource_version: str,
    plan_id: str,
    correlation_id: str | None = None,
    has_annotations: bool = True,
    requested_by: str | None = None,
) -> list[dict[str, Any]]:
    """A JSON Patch (the client sends a list body as application/json-patch+json) that
    writes exactly `template` as the pod template. The resourceVersion op comes first: a
    stale value makes the API server reject the patch with 409, as in the merge patches."""
    annotations = _annotations(plan_id, correlation_id, requested_by)
    ops: list[dict[str, Any]] = [
        {"op": "replace", "path": "/metadata/resourceVersion", "value": resource_version},
        {"op": "replace", "path": "/spec/template", "value": template},
    ]
    if not has_annotations:
        # `add` on a key needs the map to exist.
        return [*ops, {"op": "add", "path": "/metadata/annotations", "value": annotations}]
    # `add` on an existing key replaces its value (RFC 6902).
    return ops + [
        {"op": "add", "path": f"/metadata/annotations/{_pointer(key)}", "value": value}
        for key, value in annotations.items()
    ]


def _api_error(exc: ApiException) -> GuardError:
    if exc.status == 409:
        return GuardError(CONFLICT, "Deployment changed since the plan was made")
    if exc.status == 404:
        return GuardError(NOT_FOUND, "Deployment not found")
    logger.error("Kubernetes API error: status=%s reason=%s", exc.status, exc.reason)
    return GuardError(KUBERNETES_ERROR, f"Kubernetes API error ({exc.status})")


def _read(apps: k8s.AppsV1Api, namespace: str, name: str) -> Any:
    try:
        return apps.read_namespaced_deployment(name, namespace)
    except ApiException as exc:
        raise _api_error(exc) from None


def check_same_object(plan: Plan, deployment: Any) -> None:
    """Reject the apply if the Deployment was deleted and recreated since the plan was made.

    A recreated object starts again at generation 1, so generation alone cannot tell it apart.
    """
    uid = str(deployment.metadata.uid)
    if uid != plan.uid:
        raise GuardError(
            CONFLICT,
            "Deployment was replaced since the plan was made",
            {"uid": {"planned": plan.uid, "current": uid}},
        )


def check_not_gitops_managed(deployment: Any) -> None:
    """Re-run guard check 4: a label-only change does not bump generation."""
    if is_gitops_managed(deployment):
        raise GuardError(GITOPS_MANAGED, "deployment is managed by GitOps; change it in Git")


def check_spec_unchanged(plan: Plan, deployment: Any) -> None:
    """Reject the apply if the Deployment's spec changed since the plan was made.

    The API server bumps a Deployment's generation on spec and annotation changes (the
    apply itself bumps it once, by setting the plan annotation), but not on status or
    label changes.
    """
    generation = int(deployment.metadata.generation)
    replicas = deployment.spec.replicas
    replicas = DEFAULT_REPLICAS if replicas is None else replicas
    if generation != plan.generation or replicas != plan.replicas_from:
        raise GuardError(
            CONFLICT,
            "Deployment spec changed since the plan was made",
            {
                "generation": {"planned": plan.generation, "current": generation},
                "replicas": {"planned_from": plan.replicas_from, "current": replicas},
            },
        )


def check_record_matches(
    plan: Plan, action: Action, namespace: str, name: str, args: Mapping[str, Any]
) -> None:
    """The record must describe the requested change, not only carry its hash: a record
    altered in the table cannot redirect an apply to another object or change."""
    if plan.args is None:
        # Made before the argument payload was recorded; the approver says the same.
        raise GuardError(PLAN_EXPIRED, "plan predates the argument record; plan again")
    window = args.get("verify_window_seconds")
    if (
        dict(plan.args) != dict(args)
        or (plan.namespace, plan.name) != (namespace, name)
        or (window is not None and plan.verify_window_seconds != window)
        or not action.record_matches(plan, args)
    ):
        raise GuardError(PLAN_MISMATCH, "the plan record does not match the arguments")


def check_target(plan: Plan, deployment: Any) -> None:
    """All checks on the live Deployment; run before every patch attempt."""
    check_same_object(plan, deployment)
    check_not_gitops_managed(deployment)
    check_spec_unchanged(plan, deployment)


def check_breaker(
    rt: Runtime, action: Action, plan: Plan, deployment: Any, override: bool, now: int
) -> None:
    """The action's history checks, then the agent's and the global budget; re-run after the
    spec check and authoritative over the plan step."""
    action.breaker(rt, plan, deployment, override, now)
    # Last, so a request refused by another check does not use up a budget.
    take_agent_budget(rt.store, rt.settings, plan, now)
    rt.store.take_global_budget(now, rt.settings.global_hourly_apply_budget)


def _patch(
    apps: k8s.AppsV1Api,
    action: Action,
    plan: Plan,
    args: Mapping[str, Any],
    correlation_id: str | None,
    after_first_check: Callable[[Any], None],
) -> Any:
    for attempt in range(1, PATCH_ATTEMPTS + 1):
        deployment = _read(apps, plan.namespace, plan.name)
        check_target(plan, deployment)
        extra = action.extra_checks(apps, plan, deployment)
        if attempt == 1:
            # A retry only follows a 409 on an unchanged spec, so history is checked once.
            after_first_check(deployment)
        body = action.body(plan, args, deployment, extra, correlation_id)
        try:
            apps.patch_namespaced_deployment(plan.name, plan.namespace, body, dry_run="All")
            return apps.patch_namespaced_deployment(plan.name, plan.namespace, body)
        except ApiException as exc:
            if exc.status == 409 and attempt < PATCH_ATTEMPTS:
                continue
            raise _api_error(exc) from None
    raise AssertionError("unreachable")


def _record(
    rt: Runtime,
    plan_id: str,
    result: str,
    correlation_id: str | None,
    applied_generation: int | None = None,
) -> None:
    # Best effort: the change itself has already succeeded or failed.
    try:
        rt.store.record_result(plan_id, int(rt.clock()), result, correlation_id, applied_generation)
    except Exception:
        logger.exception("cannot record result for plan %s", plan_id)


def _apply(action: Action, args: Mapping[str, Any]) -> Envelope:
    rt = get_runtime()
    if not rt.kill_switch.enabled():
        raise GuardError(REMEDIATION_DISABLED, "remediation is disabled")

    s = rt.settings
    # Who is applying: in identity mode a gateway apply must come from the plan's agent.
    expected_requester = guard_apply(s)
    namespace, name, request = action.validate(args, s)
    override = request.get("override_manual_change", False) is True
    plan_id = require_plan_id(args.get("plan_id"))

    now = int(rt.clock())
    correlation_id = current_correlation_id()
    plan = rt.store.consume(
        plan_id,
        plan_args_hash(action.name, request),
        action.name,
        now,
        require_approval=s.approval_required,
        requester=expected_requester,
        channel=current_channel(),
        verify_window=s.verify_window_default_seconds if s.verify_enabled else None,
    )

    try:
        check_plan_agent(s, rt.ssm, plan)
        check_record_matches(plan, action, namespace, name, request)
        action.check_plan(rt, plan)
        updated = _patch(
            rt.apps(),
            action,
            plan,
            request,
            correlation_id,
            lambda deployment: check_breaker(rt, action, plan, deployment, override, now),
        )
    except GuardError as exc:
        _record(rt, plan_id, exc.code, correlation_id)
        raise
    except Exception:
        _record(rt, plan_id, INTERNAL, correlation_id)
        raise
    _record(rt, plan_id, RESULT_APPLIED, correlation_id, _generation(updated))

    return ok(
        {
            "plan_id": plan_id,
            "namespace": namespace,
            "name": name,
            **action.result(plan, request),
            "resource_version": updated.metadata.resource_version,
            # The approver's identity stays in the plan record and the logs.
            "approved": plan.approved_by is not None,
            "correlation_id": correlation_id,
        }
    )


def _generation(deployment: Any) -> int | None:
    generation = getattr(getattr(deployment, "metadata", None), "generation", None)
    return generation if isinstance(generation, int) else None


def _verify_request(
    args: Mapping[str, Any], settings: Settings, revert_allowed: bool = False
) -> tuple[int | None, bool]:
    return require_verify_args(
        args, settings.verify_enabled, settings.verify_window_bounds, revert_allowed
    )


# --- scale_deployment ---------------------------------------------------------


def _validate_scale(args: Mapping[str, Any], settings: Settings) -> tuple[str, str, dict[str, Any]]:
    namespace, name, replicas = validate_scale_args(
        args, settings.allowed_namespaces, settings.bounds_for
    )
    override = require_override_flag(args)
    window, revert = _verify_request(args, settings, revert_allowed=True)
    return namespace, name, scale_args(namespace, name, replicas, override, revert, window)


def _check_scale_plan(rt: Runtime, plan: Plan) -> None:
    # Re-checked against the current setting; the plan step is not trusted.
    check_replica_step(plan.replicas_from, plan.replicas_to, rt.settings.max_replica_step_percent)


def _scale_breaker(rt: Runtime, plan: Plan, deployment: Any, override: bool, now: int) -> None:
    """check_spec_unchanged has confirmed that the current replica count is replicas_from."""
    last_applied = last_applied_plan(rt.store, deployment)
    baseline = replica_baseline(rt.store, rt.settings, last_applied, now)
    check_manual_change(baseline, plan.replicas_from, override)
    check_history(
        rt.store,
        plan.object_key,
        now,
        rt.settings,
        plan.replicas_from,
        plan.replicas_to,
        plan_rate=False,
        last_applied=last_applied,
    )


SCALE = Action(
    name=SCALE_DEPLOYMENT,
    validate=_validate_scale,
    # The override the approval view shows is the record field, so it must agree with args.
    record_matches=lambda plan, args: (
        plan.replicas_to == args["replicas"]
        and plan.override_manual_change == (args.get("override_manual_change") is True)
    ),
    check_plan=_check_scale_plan,
    breaker=_scale_breaker,
    body=lambda plan, args, deployment, extra, correlation_id: scale_patch(
        args["replicas"],
        deployment.metadata.resource_version,
        plan.plan_id,
        correlation_id,
        agent_requester(plan),
    ),
    result=lambda plan, args: {
        "replicas_from": plan.replicas_from,
        "replicas_to": args["replicas"],
    },
)


def apply_scale_deployment(args: Mapping[str, Any]) -> Envelope:
    return _apply(SCALE, args)


# --- restart_deployment -------------------------------------------------------


def _validate_restart(
    args: Mapping[str, Any], settings: Settings
) -> tuple[str, str, dict[str, Any]]:
    namespace, name = validate_target_args(args, settings.allowed_namespaces)
    window, _ = _verify_request(args, settings)
    return namespace, name, restart_args(namespace, name, window)


def _restart_breaker(rt: Runtime, plan: Plan, deployment: Any, override: bool, now: int) -> None:
    """No manual-change check (a restart undoes no change) and no direction (replicas stay)."""
    check_history(
        rt.store,
        plan.object_key,
        now,
        rt.settings,
        plan.replicas_from,
        plan.replicas_to,
        plan_rate=False,
        last_applied=last_applied_plan(rt.store, deployment),
    )


RESTART = Action(
    name=RESTART_DEPLOYMENT,
    validate=_validate_restart,
    # A restart record keeps the replica count and pins the template it leaves.
    record_matches=lambda plan, args: (
        plan.replicas_to == plan.replicas_from
        and plan.template_to is not None
        and not plan.override_manual_change
    ),
    # No blast-radius step: replicas do not change (docs/guards.md).
    check_plan=_no_checks,
    breaker=_restart_breaker,
    body=lambda plan, args, deployment, extra, correlation_id: restart_patch(
        deployment.metadata.resource_version, plan.plan_id, correlation_id, agent_requester(plan)
    ),
    result=lambda plan, args: {"pods_replaced": plan.replicas_to, "template": plan.template_to},
    extra_checks=lambda apps, plan, deployment: check_not_paused(deployment),
)


def apply_restart_deployment(args: Mapping[str, Any]) -> Envelope:
    return _apply(RESTART, args)


# --- rollback_deployment ------------------------------------------------------


def _validate_rollback(
    args: Mapping[str, Any], settings: Settings
) -> tuple[str, str, dict[str, Any]]:
    namespace, name = validate_target_args(args, settings.allowed_namespaces)
    revision = require_revision(args.get("revision"))
    override = require_override_flag(args)
    window, _ = _verify_request(args, settings)
    return namespace, name, rollback_args(namespace, name, revision, override, window)


def _read_target_template(apps: k8s.AppsV1Api, plan: Plan, deployment: Any) -> dict[str, Any]:
    """Re-read the pinned ReplicaSet as raw JSON and return its template, minus the
    pod-template-hash label, only if it is still the one the plan pinned and approved."""
    check_not_paused(deployment)
    name = str(plan.target_rs_name)
    try:
        response = apps.read_namespaced_replica_set(name, plan.namespace, _preload_content=False)
    except ApiException as exc:
        if exc.status == 404:
            raise GuardError(
                REVISION_NOT_FOUND,
                f"ReplicaSet {name} of revision {plan.revision_to} no longer exists",
                {"revision": plan.revision_to, "replica_set": name},
            ) from None
        raise _api_error(exc) from None
    rs = json.loads(response.data)
    uid = str((rs.get("metadata") or {}).get("uid") or "")
    if uid != plan.target_rs_uid or not is_controlled_by(rs, plan.uid):
        raise GuardError(
            CONFLICT,
            f"ReplicaSet {name} is no longer the one the plan pinned",
            {"replica_set": {"planned": plan.target_rs_uid, "current": uid}},
        )
    template = template_of(rs)
    current = template_hash(template)
    if current != plan.template_to:
        raise GuardError(
            CONFLICT,
            f"the template of ReplicaSet {name} changed since the plan was made",
            {"template": {"planned": plan.template_to, "current": current}},
        )
    # Exactly what was hashed and compared is what the patch writes.
    return template


def _rollback_breaker(rt: Runtime, plan: Plan, deployment: Any, override: bool, now: int) -> None:
    """check_spec_unchanged has confirmed that generation is unchanged, so the Deployment's
    template is still the one hashed as template_from."""
    last_applied = last_applied_plan(rt.store, deployment)
    check_template_manual_change(last_applied, str(plan.template_from), override)
    check_history(
        rt.store,
        plan.object_key,
        now,
        rt.settings,
        plan.replicas_from,
        plan.replicas_to,
        plan_rate=False,
        last_applied=last_applied,
    )


ROLLBACK = Action(
    name=ROLLBACK_DEPLOYMENT,
    validate=_validate_rollback,
    # A rollback record keeps the replica count, pins the ReplicaSet and its template, and
    # carries the override the approval view shows.
    record_matches=lambda plan, args: (
        plan.revision_to == args["revision"]
        and plan.replicas_to == plan.replicas_from
        and None not in (plan.target_rs_name, plan.target_rs_uid, plan.template_from)
        and plan.template_to is not None
        and plan.override_manual_change == (args.get("override_manual_change") is True)
    ),
    check_plan=_no_checks,
    breaker=_rollback_breaker,
    body=lambda plan, args, deployment, template, correlation_id: rollback_patch(
        template,
        deployment.metadata.resource_version,
        plan.plan_id,
        correlation_id,
        has_annotations=bool(deployment.metadata.annotations),
        requested_by=agent_requester(plan),
    ),
    result=lambda plan, args: {
        "revision_requested": args["revision"],
        "template": plan.template_to,
        "images": plan.images,
    },
    extra_checks=_read_target_template,
)


def apply_rollback_deployment(args: Mapping[str, Any]) -> Envelope:
    return _apply(ROLLBACK, args)


# --- verification (internal) --------------------------------------------------


def _verdict(outcome: str, reason: str | None, now: int, deployment: Any = None) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "reason": reason,
        "checked_at": now,
        "observed": verify.observed(deployment) if deployment is not None else None,
        "correlation_id": current_correlation_id(),
    }


def _check(rt: Runtime, plan: Plan, now: int) -> tuple[dict[str, Any], Any]:
    """One reading of the Deployment, evaluated; returns the verdict and the Deployment it
    read, if any. A plan that was consumed but whose apply did not patch is not_applied; past
    the grace period nothing is read (late)."""
    if not plan.used:
        return _verdict(verify.NOT_APPLIED, None, now), None
    if _late(rt, plan, now):
        # A verdict long after the approved window is not what was approved.
        return _verdict(verify.UNKNOWN, verify.LATE, now), None
    try:
        deployment = rt.apps().read_namespaced_deployment(plan.name, plan.namespace)
    except ApiException as exc:
        logger.error("Kubernetes API error: status=%s reason=%s", exc.status, exc.reason)
        reason = verify.NOT_FOUND if exc.status == 404 else verify.KUBERNETES_ERROR
        return _verdict(verify.UNKNOWN, reason, now), None
    failed = plan.result is not None and plan.result != RESULT_APPLIED
    if failed and last_plan_id(deployment) != plan.plan_id:
        # The apply was refused before its patch (only a successful patch sets the annotation).
        return _verdict(verify.NOT_APPLIED, None, now), None
    outcome, reason = verify.evaluate(plan, deployment)
    return _verdict(outcome, reason, now, deployment), deployment


def _late(rt: Runtime, plan: Plan, now: int) -> bool:
    return now > (plan.verify_at or 0) + rt.settings.verify_grace_seconds


def _revert_state(
    status: str, reason: str | None, now: int, revert_id: str | None = None
) -> dict[str, Any]:
    return {"status": status, "reason": reason, "revert_plan_id": revert_id, "at": now}


def revert_record(rt: Runtime, plan: Plan, now: int) -> Plan:
    """The record of the revert of `plan`: a full plan record that restores replicas_from,
    so history, the manual-change check and the next plan see it as the last apply. It can
    never be requested, approved or applied (used, status revert, a system requester), and
    it is verified one window later but never reverted (no revert_on_failure)."""
    args = scale_args(plan.namespace, plan.name, plan.replicas_from)
    window = plan.verify_window_seconds or rt.settings.verify_window_default_seconds
    return Plan(
        plan_id=revert_plan_id(plan.plan_id),
        action=SCALE_DEPLOYMENT,
        namespace=plan.namespace,
        name=plan.name,
        replicas_from=plan.replicas_to,
        replicas_to=plan.replicas_from,
        args_hash=plan_args_hash(SCALE_DEPLOYMENT, args),
        args=args,
        resource_version=plan.resource_version,
        # The spec the revert expects to find: the one the parent's apply left.
        generation=plan.generation + 1,
        uid=plan.uid,
        created_at=now,
        expires_at=now,
        retain_until=now + retention_seconds(rt.settings),
        used=True,
        requested_by=REQUESTER_REVERT,
        status=STATUS_REVERT,
        agent=plan.agent,
        requested_client_id=plan.requested_client_id,
        channel=CHANNEL_VERIFIER,
        plan_correlation_id=current_correlation_id(),
        template_from=plan.template_to,
        template_to=plan.template_to,
        verify_window_seconds=window,
        verify_at=now + window,
        kind=KIND_REVERT,
        reverts_plan_id=plan.plan_id,
    )


def _patch_revert(
    apps: k8s.AppsV1Api, plan: Plan, revert: Plan, deployment: Any, correlation_id: str | None
) -> Any:
    """Patch replicas back to the parent's replicas_from, only on the spec the parent's
    apply left: same uid, still annotated with the parent, generation + 1, replicas_to (the
    revert record's own generation and replicas_from), and not GitOps-managed, with the
    resourceVersion of that same reading. A Deployment already annotated with the revert
    was patched by an earlier run that stopped before recording it."""
    for attempt in range(1, PATCH_ATTEMPTS + 1):
        if deployment is None or attempt > 1:
            deployment = _read(apps, plan.namespace, plan.name)
        if last_plan_id(deployment) == revert.plan_id:
            return deployment
        if last_plan_id(deployment) != plan.plan_id:
            raise GuardError(CONFLICT, "Deployment changed since the apply it would revert")
        check_target(revert, deployment)
        body = scale_patch(
            revert.replicas_to,
            deployment.metadata.resource_version,
            revert.plan_id,
            correlation_id,
            revert.requested_by,
        )
        try:
            apps.patch_namespaced_deployment(plan.name, plan.namespace, body, dry_run="All")
            return apps.patch_namespaced_deployment(plan.name, plan.namespace, body)
        except ApiException as exc:
            if exc.status == 409 and attempt < PATCH_ATTEMPTS:
                continue
            raise _api_error(exc) from None
    raise AssertionError("unreachable")


def _revert(rt: Runtime, plan: Plan, now: int, deployment: Any) -> dict[str, Any] | None:
    """Undo a degraded change that was approved with revert_on_failure. Only the global kill
    switch, the replica bounds, GitOps ownership and the spec precondition can stop it; it
    takes no budget unit and runs no history check (mcp/CONTRACT.md, Verification and
    revert). Returns the final revert state, or None to try again on the next run; any
    other failure also leaves the plan due, and verify_applied_plan ends it once late."""
    revert_id = revert_plan_id(plan.plan_id)
    if deployment is None:
        try:
            deployment = _read(rt.apps(), plan.namespace, plan.name)
        except GuardError as exc:
            if exc.code != NOT_FOUND:
                raise
            return _revert_state(verify.REVERT_REFUSED, NOT_FOUND, now)
    if last_plan_id(deployment) == revert_id:
        # An earlier run's patch landed and that run stopped before recording it: whatever
        # changed since, the revert is done.
        record = rt.store.get(revert_id) or {}
        if record.get("result") != RESULT_APPLIED:
            _record(rt, revert_id, RESULT_APPLIED, current_correlation_id())
        return _revert_state(verify.REVERTED, None, now, revert_id)
    if _late(rt, plan, now):
        # A revert long after the approved window is not what was approved.
        return _revert_state(verify.REVERT_SKIPPED, verify.LATE, now)
    switch = rt.kill_switch.state()
    if switch is None:
        # Unreadable: a transient error must not cancel a pre-approved revert for good.
        return None
    if not switch:
        # Final: by the time the switch is back on, the approved window has long passed.
        return _revert_state(verify.REVERT_SKIPPED, REMEDIATION_DISABLED, now)
    minimum, maximum = rt.settings.bounds_for(plan.namespace)
    if not minimum <= plan.replicas_from <= maximum:
        return _revert_state(verify.REVERT_REFUSED, OUT_OF_BOUNDS, now)
    revert = revert_record(rt, plan, now)
    rt.store.create_revert(revert)
    correlation_id = current_correlation_id()
    try:
        updated = _patch_revert(rt.apps(), plan, revert, deployment, correlation_id)
    except GuardError as exc:
        _record(rt, revert.plan_id, exc.code, correlation_id)
        return _revert_state(verify.REVERT_REFUSED, exc.code, now, revert.plan_id)
    _record(rt, revert.plan_id, RESULT_APPLIED, correlation_id, _generation(updated))
    return _revert_state(verify.REVERTED, None, now, revert.plan_id)


def verify_applied_plan(args: Mapping[str, Any]) -> Envelope:
    """Verify one plan whose verify_at has passed, and revert it if it is degraded and was
    approved with revert_on_failure (mcp/CONTRACT.md, Verification and revert). Only the
    verifier channel reaches it. Every write is conditional, so a run that repeats an
    earlier one, or overlaps it, changes nothing; the plan leaves the verify-due index only
    once its verification and revert are final."""
    rt = get_runtime()
    plan_id = require_plan_id(args.get("plan_id"))
    now = int(rt.clock())
    item = rt.store.get(plan_id)
    plan = Plan.from_item(item) if item is not None else None
    if plan is None or plan.verify_at is None or plan.verify_at > now:
        return ok({"plan_id": plan_id, "due": False})
    verification, deployment = plan.verification, None
    if verification is None:
        verdict, deployment = _check(rt, plan, now)
        verification = rt.store.record_once(plan_id, "verification", verdict)
    revert = plan.revert
    wanted = plan.revert_on_failure and plan.kind != KIND_REVERT
    if revert is None and wanted and verification.get("outcome") == verify.DEGRADED:
        try:
            state = _revert(rt, plan, now, deployment)
        except Exception:
            if not _late(rt, plan, now):
                raise
            # Past the grace period every failure is final, as an unreadable switch is.
            logger.exception("revert of plan %s failed after its grace period", plan_id)
            state = _revert_state(verify.REVERT_SKIPPED, verify.LATE, now)
        if state is None:
            return ok(_verified(plan_id, verification, verify.REVERT_PENDING))
        revert = rt.store.record_once(plan_id, "revert", state)
    rt.store.finish_verification(plan_id)
    return ok(_verified(plan_id, verification, (revert or {}).get("status")))


def _verified(plan_id: str, verification: Mapping[str, Any], revert: str | None) -> dict:
    return {
        "plan_id": plan_id,
        "due": True,
        "outcome": verification.get("outcome"),
        "reason": verification.get("reason"),
        "revert": revert,
    }


HANDLERS = {
    "apply_scale_deployment": apply_scale_deployment,
    "apply_restart_deployment": apply_restart_deployment,
    "apply_rollback_deployment": apply_rollback_deployment,
}
# Reached only as kh_internal___<name> on the verifier channel (handrail_common.dispatch);
# scripts/check_contract.py keeps these names out of every tools.json.
INTERNAL_HANDLERS = {
    "verify_applied_plan": verify_applied_plan,
}


def lambda_handler(event: Any, context: Any) -> Envelope:
    return dispatch(HANDLERS, event, context, internal=INTERNAL_HANDLERS)
