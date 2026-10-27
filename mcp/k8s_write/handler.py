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
    INTERNAL,
    KUBERNETES_ERROR,
    NOT_FOUND,
    REMEDIATION_DISABLED,
    Envelope,
    ok,
)
from handrail_common.killswitch import KillSwitch
from handrail_common.plans import SCALE_DEPLOYMENT, PlanStore, scale_args_hash
from handrail_common.validation import GuardError, require_plan_id, validate_scale_args

logger = logging.getLogger(__name__)

LAST_PLAN_ANNOTATION = "kube-handrail.io/last-plan-id"
RESULT_APPLIED = "applied"


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
    # resourceVersion in the patch body makes the API server reject it with 409 on drift.
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


def _patch(apps: k8s.AppsV1Api, namespace: str, name: str, body: dict[str, Any]) -> Any:
    try:
        apps.patch_namespaced_deployment(name, namespace, body, dry_run="All")
        return apps.patch_namespaced_deployment(name, namespace, body)
    except ApiException as exc:
        raise _api_error(exc) from None


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

    body = scale_patch(replicas, plan.resource_version, plan_id)
    try:
        updated = _patch(rt.apps(), namespace, name, body)
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
