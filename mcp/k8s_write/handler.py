"""k8s_write Lambda: apply a previously planned Deployment scale change."""

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import boto3
from kubernetes import client as k8s
from kubernetes.client.exceptions import ApiException

from handrail_common.config import ConfigError, Settings, load_settings
from handrail_common.dispatch import dispatch
from handrail_common.eks import api_client
from handrail_common.envelope import (
    CONFLICT,
    GITOPS_MANAGED,
    INTERNAL,
    KUBERNETES_ERROR,
    NOT_FOUND,
    REMEDIATION_DISABLED,
    Envelope,
    ok,
)
from handrail_common.gitops import is_gitops_managed
from handrail_common.killswitch import KillSwitch
from handrail_common.plans import SCALE_DEPLOYMENT, Plan, PlanStore, scale_args_hash
from handrail_common.validation import GuardError, require_plan_id, validate_scale_args

logger = logging.getLogger(__name__)

LAST_PLAN_ANNOTATION = "kube-handrail.io/last-plan-id"
RESULT_APPLIED = "applied"
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


def scale_patch(replicas: int, resource_version: str, plan_id: str) -> dict[str, Any]:
    # resourceVersion in the patch body makes the API server reject it with 409 if the
    # object changed between the spec check and the patch.
    return {
        "metadata": {
            "resourceVersion": resource_version,
            "annotations": {LAST_PLAN_ANNOTATION: plan_id},
        },
        "spec": {"replicas": replicas},
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


def check_target(plan: Plan, deployment: Any) -> None:
    """All checks on the live Deployment; run before every patch attempt."""
    check_same_object(plan, deployment)
    check_not_gitops_managed(deployment)
    check_spec_unchanged(plan, deployment)


def _patch(apps: k8s.AppsV1Api, plan: Plan, replicas: int) -> Any:
    for attempt in range(1, PATCH_ATTEMPTS + 1):
        deployment = _read(apps, plan.namespace, plan.name)
        check_target(plan, deployment)
        body = scale_patch(replicas, deployment.metadata.resource_version, plan.plan_id)
        try:
            apps.patch_namespaced_deployment(plan.name, plan.namespace, body, dry_run="All")
            return apps.patch_namespaced_deployment(plan.name, plan.namespace, body)
        except ApiException as exc:
            if exc.status == 409 and attempt < PATCH_ATTEMPTS:
                continue
            raise _api_error(exc) from None
    raise AssertionError("unreachable")


def _record(rt: Runtime, plan_id: str, result: str) -> None:
    # Best effort: the change itself has already succeeded or failed.
    try:
        rt.store.record_result(plan_id, int(rt.clock()), result)
    except Exception:
        logger.exception("cannot record result for plan %s", plan_id)


def apply_scale_deployment(args: Mapping[str, Any]) -> Envelope:
    rt = get_runtime()
    if not rt.kill_switch.enabled():
        raise GuardError(REMEDIATION_DISABLED, "remediation is disabled")

    s = rt.settings
    namespace, name, replicas = validate_scale_args(
        args, s.allowed_namespaces, s.min_replicas, s.max_replicas
    )
    plan_id = require_plan_id(args.get("plan_id"))

    plan = rt.store.consume(
        plan_id, scale_args_hash(namespace, name, replicas), SCALE_DEPLOYMENT, int(rt.clock())
    )

    try:
        updated = _patch(rt.apps(), plan, replicas)
    except GuardError as exc:
        _record(rt, plan_id, exc.code)
        raise
    except Exception:
        _record(rt, plan_id, INTERNAL)
        raise
    _record(rt, plan_id, RESULT_APPLIED)

    return ok(
        {
            "plan_id": plan_id,
            "namespace": namespace,
            "name": name,
            "replicas_from": plan.replicas_from,
            "replicas_to": replicas,
            "resource_version": updated.metadata.resource_version,
        }
    )


HANDLERS = {"apply_scale_deployment": apply_scale_deployment}


def lambda_handler(event: Any, context: Any) -> Envelope:
    return dispatch(HANDLERS, event, context)
