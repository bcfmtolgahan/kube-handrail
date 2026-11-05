"""k8s_write Lambda: apply a previously planned Deployment change."""

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import boto3
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

from handrail_common.breaker import (
    LAST_PLAN_ANNOTATION,
    check_history,
    check_manual_change,
    last_applied_plan,
    replica_baseline,
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
    PLAN_EXPIRED,
    PLAN_MISMATCH,
    REMEDIATION_DISABLED,
    Envelope,
    ok,
)
from handrail_common.gitops import is_gitops_managed
from handrail_common.killswitch import KillSwitch
from handrail_common.observability import CORRELATION_ANNOTATION, current_correlation_id
from handrail_common.plans import (
    RESTART_ANNOTATION,
    RESTART_DEPLOYMENT,
    RESULT_APPLIED,
    SCALE_DEPLOYMENT,
    Plan,
    PlanStore,
    plan_args_hash,
    restart_args,
    scale_args,
)
from handrail_common.validation import (
    GuardError,
    check_not_paused,
    check_replica_step,
    require_override_flag,
    require_plan_id,
    validate_scale_args,
    validate_target_args,
)

logger = logging.getLogger(__name__)

DEFAULT_REPLICAS = 1
# Status updates (for example during a rollout) change resourceVersion but not the spec.
# A 409 caused by one is retried after re-checking the object; a spec change never is.
PATCH_ATTEMPTS = 3


@dataclass
class Runtime:
    settings: Settings
    kill_switch: KillSwitch
    store: PlanStore
    apps: Callable[[], k8s.AppsV1Api]
    clock: Callable[[], float] = time.time


_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    """Build dependencies once per execution environment."""
    global _runtime
    if _runtime is None:
        settings = load_settings(write=True)
        param = settings.remediation_enabled_param
        if param is None:
            raise ConfigError("REMEDIATION_ENABLED_PARAM is required")
        _runtime = Runtime(
            settings=settings,
            kill_switch=KillSwitch(boto3.client("ssm", region_name=settings.region), param),
            store=PlanStore.from_name(settings.plans_table, settings.region),
            apps=lambda: k8s.AppsV1Api(api_client(settings.cluster_name)),
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
    # The patch body for the Deployment just read and checked.
    body: Callable[[Plan, Mapping[str, Any], Any, str | None], Any]
    # Action-specific fields of the response data.
    result: Callable[[Plan, Mapping[str, Any]], dict[str, Any]]
    # Further checks on the live Deployment, after the generic ones, before every attempt.
    extra_checks: Callable[[Plan, Any], None] = _no_checks


def _patch_metadata(
    resource_version: str, plan_id: str, correlation_id: str | None
) -> dict[str, Any]:
    # resourceVersion in the patch body makes the API server reject it with 409 if the
    # object changed between the spec check and the patch.
    annotations = {LAST_PLAN_ANNOTATION: plan_id}
    if correlation_id is not None:
        annotations[CORRELATION_ANNOTATION] = correlation_id
    return {"resourceVersion": resource_version, "annotations": annotations}


def scale_patch(
    replicas: int, resource_version: str, plan_id: str, correlation_id: str | None = None
) -> dict[str, Any]:
    return {
        "metadata": _patch_metadata(resource_version, plan_id, correlation_id),
        "spec": {"replicas": replicas},
    }


def restart_patch(
    resource_version: str, plan_id: str, correlation_id: str | None = None
) -> dict[str, Any]:
    """Only the restart annotation changes in the pod template, so the template written is
    the one the plan pinned (template_to)."""
    return {
        "metadata": _patch_metadata(resource_version, plan_id, correlation_id),
        "spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: plan_id}}}},
    }


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
    if (
        dict(plan.args) != dict(args)
        or (plan.namespace, plan.name) != (namespace, name)
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
    """The action's history checks, then the global budget; re-run after the spec check and
    authoritative over the plan step."""
    action.breaker(rt, plan, deployment, override, now)
    # Last, so a request refused by another check does not use up the budget.
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
        action.extra_checks(plan, deployment)
        if attempt == 1:
            # A retry only follows a 409 on an unchanged spec, so history is checked once.
            after_first_check(deployment)
        body = action.body(plan, args, deployment, correlation_id)
        try:
            apps.patch_namespaced_deployment(plan.name, plan.namespace, body, dry_run="All")
            return apps.patch_namespaced_deployment(plan.name, plan.namespace, body)
        except ApiException as exc:
            if exc.status == 409 and attempt < PATCH_ATTEMPTS:
                continue
            raise _api_error(exc) from None
    raise AssertionError("unreachable")


def _record(rt: Runtime, plan_id: str, result: str, correlation_id: str | None) -> None:
    # Best effort: the change itself has already succeeded or failed.
    try:
        rt.store.record_result(plan_id, int(rt.clock()), result, correlation_id)
    except Exception:
        logger.exception("cannot record result for plan %s", plan_id)


def _apply(action: Action, args: Mapping[str, Any]) -> Envelope:
    rt = get_runtime()
    if not rt.kill_switch.enabled():
        raise GuardError(REMEDIATION_DISABLED, "remediation is disabled")

    s = rt.settings
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
    )

    try:
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
    _record(rt, plan_id, RESULT_APPLIED, correlation_id)

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


# --- scale_deployment ---------------------------------------------------------


def _validate_scale(args: Mapping[str, Any], settings: Settings) -> tuple[str, str, dict[str, Any]]:
    namespace, name, replicas = validate_scale_args(
        args, settings.allowed_namespaces, settings.bounds_for
    )
    override = require_override_flag(args)
    return namespace, name, scale_args(namespace, name, replicas, override)


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
    body=lambda plan, args, deployment, correlation_id: scale_patch(
        args["replicas"], deployment.metadata.resource_version, plan.plan_id, correlation_id
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
    return namespace, name, restart_args(namespace, name)


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
    body=lambda plan, args, deployment, correlation_id: restart_patch(
        deployment.metadata.resource_version, plan.plan_id, correlation_id
    ),
    result=lambda plan, args: {"pods_replaced": plan.replicas_to, "template": plan.template_to},
    extra_checks=lambda plan, deployment: check_not_paused(deployment),
)


def apply_restart_deployment(args: Mapping[str, Any]) -> Envelope:
    return _apply(RESTART, args)


HANDLERS = {
    "apply_scale_deployment": apply_scale_deployment,
    "apply_restart_deployment": apply_restart_deployment,
}


def lambda_handler(event: Any, context: Any) -> Envelope:
    return dispatch(HANDLERS, event, context)
