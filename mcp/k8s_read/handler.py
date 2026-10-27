"""k8s_read Lambda: Deployment status and scale planning. Never mutates the cluster."""

import time
import uuid
from functools import cache
from typing import Any

from kubernetes import client as k8s
from kubernetes.client.rest import ApiException

from handrail_common.config import Settings, load_settings
from handrail_common.dispatch import dispatch
from handrail_common.eks import api_client
from handrail_common.envelope import (
    CONFLICT,
    GITOPS_MANAGED,
    KUBERNETES_ERROR,
    NOT_FOUND,
    Envelope,
    ok,
)
from handrail_common.gitops import is_gitops_managed
from handrail_common.plans import SCALE_DEPLOYMENT, Plan, PlanStore, scale_args_hash
from handrail_common.validation import (
    GuardError,
    check_namespace_allowed,
    require_name,
    require_namespace,
    validate_scale_args,
)

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
# The API server defaults spec.replicas to 1 when it is omitted.
DEFAULT_REPLICAS = 1


def _kubernetes_error(exc: ApiException, what: str) -> GuardError:
    if exc.status == 404:
        return GuardError(NOT_FOUND, f"{what} not found")
    # Status and reason only; the response body can echo cluster internals.
    reason = exc.reason or "unknown"
    return GuardError(KUBERNETES_ERROR, f"kubernetes API error {exc.status}: {reason}")


def _read_deployment(apps: k8s.AppsV1Api, namespace: str, name: str) -> Any:
    try:
        return apps.read_namespaced_deployment(name=name, namespace=namespace)
    except ApiException as exc:
        raise _kubernetes_error(exc, f"deployment {namespace}/{name}") from None


def _targeting_hpas(autoscaling: k8s.AutoscalingV2Api, namespace: str, name: str) -> list[str]:
    try:
        hpas = autoscaling.list_namespaced_horizontal_pod_autoscaler(namespace=namespace)
    except ApiException as exc:
        raise _kubernetes_error(exc, f"namespace {namespace}") from None
    # Matched on kind and name only, regardless of apiVersion, to fail closed.
    return [
        hpa.metadata.name
        for hpa in hpas.items or []
        if hpa.spec
        and hpa.spec.scale_target_ref
        and hpa.spec.scale_target_ref.kind == "Deployment"
        and hpa.spec.scale_target_ref.name == name
    ]


def _desired_replicas(deployment: Any) -> int:
    replicas = deployment.spec.replicas if deployment.spec else None
    return DEFAULT_REPLICAS if replicas is None else replicas


def _conditions(status: Any) -> list[dict[str, Any]]:
    return [
        {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
        for c in (status.conditions if status else None) or []
    ]


def _summary(deployment: Any) -> dict[str, Any]:
    meta = deployment.metadata
    status = deployment.status
    return {
        "namespace": meta.namespace,
        "name": meta.name,
        "replicas": {
            "desired": _desired_replicas(deployment),
            "ready": (status.ready_replicas if status else None) or 0,
            "updated": (status.updated_replicas if status else None) or 0,
            "available": (status.available_replicas if status else None) or 0,
        },
        "conditions": _conditions(status),
        "revision": (meta.annotations or {}).get(REVISION_ANNOTATION),
        "resource_version": meta.resource_version,
        "gitops_managed": is_gitops_managed(deployment),
    }


class Clients:
    """API clients, built on first use so argument checks never reach AWS or the cluster."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def apps(self) -> k8s.AppsV1Api:
        return k8s.AppsV1Api(api_client(self._settings.cluster_name))

    @property
    def autoscaling(self) -> k8s.AutoscalingV2Api:
        return k8s.AutoscalingV2Api(api_client(self._settings.cluster_name))

    @property
    def store(self) -> PlanStore:
        return _store(self._settings.plans_table, self._settings.region)


@cache
def _store(table: str, region: str | None) -> PlanStore:
    return PlanStore.from_name(table, region)


def get_workload_status(args: dict[str, Any], settings: Settings, clients: Clients) -> Envelope:
    namespace = require_namespace(args.get("namespace"))
    name = require_name(args.get("name"))
    check_namespace_allowed(namespace, settings.allowed_namespaces)
    return ok(_summary(_read_deployment(clients.apps, namespace, name)))


def plan_scale_deployment(
    args: dict[str, Any], settings: Settings, clients: Clients, now: int
) -> Envelope:
    namespace, name, replicas = validate_scale_args(
        args, settings.allowed_namespaces, settings.min_replicas, settings.max_replicas
    )
    deployment = _read_deployment(clients.apps, namespace, name)
    if is_gitops_managed(deployment):
        raise GuardError(GITOPS_MANAGED, "deployment is managed by GitOps; change it in Git")
    hpas = _targeting_hpas(clients.autoscaling, namespace, name)
    if hpas:
        raise GuardError(
            CONFLICT, f"HorizontalPodAutoscaler {', '.join(sorted(hpas))} targets this deployment"
        )

    plan = Plan(
        plan_id=str(uuid.uuid4()),
        action=SCALE_DEPLOYMENT,
        namespace=namespace,
        name=name,
        replicas_from=_desired_replicas(deployment),
        replicas_to=replicas,
        args_hash=scale_args_hash(namespace, name, replicas),
        resource_version=str(deployment.metadata.resource_version),
        generation=int(deployment.metadata.generation),
        uid=str(deployment.metadata.uid),
        created_at=now,
        expires_at=now + settings.plan_ttl_seconds,
    )
    clients.store.create(plan)
    checks = ("arguments", "namespace_allowed", "replica_bounds", "not_gitops_managed", "no_hpa")
    return ok(
        {
            "plan_id": plan.plan_id,
            "diff": {"replicas": {"from": plan.replicas_from, "to": plan.replicas_to}},
            "preflight": [{"check": check, "passed": True} for check in checks],
            "expires_at": plan.expires_at,
        }
    )


@cache
def _settings() -> Settings:
    return load_settings()


HANDLERS = {
    "get_workload_status": lambda args: get_workload_status(
        args, _settings(), Clients(_settings())
    ),
    "plan_scale_deployment": lambda args: plan_scale_deployment(
        args, _settings(), Clients(_settings()), int(time.time())
    ),
}


def lambda_handler(event: Any, context: Any) -> Envelope:
    return dispatch(HANDLERS, event, context)
