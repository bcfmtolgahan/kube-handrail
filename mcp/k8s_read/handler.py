"""k8s_read Lambda: Deployment status and scale planning. Never mutates the cluster."""

import json
import time
import uuid
from collections.abc import Callable
from functools import cache, partial
from typing import Any

import boto3
from kubernetes import client as k8s
from kubernetes.client.rest import ApiException

from handrail_common.approval import notification, plain, plan_view
from handrail_common.breaker import (
    check_history,
    check_manual_change,
    last_applied_plan,
    retention_seconds,
)
from handrail_common.config import Settings, load_settings
from handrail_common.dispatch import dispatch
from handrail_common.eks import api_client
from handrail_common.envelope import (
    CONFLICT,
    GITOPS_MANAGED,
    INTERNAL,
    INVALID_ARGUMENT,
    KUBERNETES_ERROR,
    NOT_FOUND,
    Envelope,
    ok,
)
from handrail_common.gitops import is_gitops_managed
from handrail_common.impact import scale_impact
from handrail_common.observability import current_correlation_id
from handrail_common.plans import (
    GLOBAL_BUDGET_WINDOW_SECONDS,
    REQUESTER_GATEWAY,
    SCALE_DEPLOYMENT,
    STATUS_AWAITING,
    STATUS_PLANNED,
    Plan,
    PlanStore,
    global_budget_error,
    object_key,
    scale_args_hash,
)
from handrail_common.validation import (
    GuardError,
    check_namespace_allowed,
    check_replica_step,
    require_name,
    require_namespace,
    require_override_flag,
    require_plan_id,
    validate_scale_args,
)

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
APPROVAL_SUBJECT = "kube-handrail: approval requested"
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


def _list(what: str, call: Callable[..., Any]) -> list[dict[str, Any]]:
    """List objects as raw JSON. The client's models refuse some objects the API server
    returns, such as an EndpointSlice with `endpoints: null` (a Service with no pods)."""
    try:
        response = call(_preload_content=False)
    except ApiException as exc:
        # Never not_found: a failed read must not look like an empty list.
        reason = exc.reason or "unknown"
        raise GuardError(
            KUBERNETES_ERROR, f"cannot list {what}: kubernetes API error {exc.status}: {reason}"
        ) from None
    return json.loads(response.data).get("items") or []


def _pod_template_labels(deployment: Any) -> dict[str, str]:
    template = deployment.spec.template if deployment.spec else None
    metadata = template.metadata if template else None
    return dict((metadata.labels if metadata else None) or {})


def _impact(
    clients: "Clients", namespace: str, deployment: Any, replicas_from: int, replicas_to: int
) -> dict[str, Any]:
    """Read what the change affects. Any read failure fails the plan: the approver must see
    facts, not gaps."""
    pdbs = _list(
        "PodDisruptionBudgets",
        partial(clients.policy.list_namespaced_pod_disruption_budget, namespace=namespace),
    )
    services = _list("Services", partial(clients.core.list_namespaced_service, namespace=namespace))
    slices = _list(
        "EndpointSlices",
        partial(clients.discovery.list_namespaced_endpoint_slice, namespace=namespace),
    )
    ready_now = (deployment.status.ready_replicas if deployment.status else None) or 0
    try:
        return scale_impact(
            _pod_template_labels(deployment),
            ready_now,
            replicas_from,
            replicas_to,
            pdbs,
            services,
            slices,
        )
    except ValueError as exc:
        # An object the preview cannot interpret, such as an unknown selector operator.
        raise GuardError(KUBERNETES_ERROR, f"cannot build the impact preview: {exc}") from None


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
    def policy(self) -> k8s.PolicyV1Api:
        return k8s.PolicyV1Api(api_client(self._settings.cluster_name))

    @property
    def core(self) -> k8s.CoreV1Api:
        return k8s.CoreV1Api(api_client(self._settings.cluster_name))

    @property
    def discovery(self) -> k8s.DiscoveryV1Api:
        return k8s.DiscoveryV1Api(api_client(self._settings.cluster_name))

    @property
    def store(self) -> PlanStore:
        return _store(self._settings.plans_table, self._settings.region)

    @property
    def sns(self) -> Any:
        return _sns(self._settings.region)


@cache
def _store(table: str, region: str | None) -> PlanStore:
    return PlanStore.from_name(table, region)


@cache
def _sns(region: str | None) -> Any:
    return boto3.client("sns", region_name=region)


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
    override = require_override_flag(args)
    deployment = _read_deployment(clients.apps, namespace, name)
    if is_gitops_managed(deployment):
        raise GuardError(GITOPS_MANAGED, "deployment is managed by GitOps; change it in Git")
    hpas = _targeting_hpas(clients.autoscaling, namespace, name)
    if hpas:
        raise GuardError(
            CONFLICT, f"HorizontalPodAutoscaler {', '.join(sorted(hpas))} targets this deployment"
        )

    current = _desired_replicas(deployment)
    check_replica_step(current, replicas, settings.max_replica_step_percent)
    uid = str(deployment.metadata.uid)
    # History checks give early feedback; apply runs all but the plan rate limit again.
    last_applied = last_applied_plan(clients.store, deployment)
    overridden = check_manual_change(last_applied, current, override)
    check_history(
        clients.store,
        object_key(namespace, name, uid),
        now,
        settings,
        current,
        replicas,
        plan_rate=True,
        last_applied=last_applied,
    )
    # Read only here; apply takes a unit of the budget atomically.
    if clients.store.global_applies(now) >= settings.global_hourly_apply_budget:
        raise global_budget_error(
            now - now % GLOBAL_BUDGET_WINDOW_SECONDS, settings.global_hourly_apply_budget
        )
    # Last, after every check has passed, and before the plan is stored.
    impact = _impact(clients, namespace, deployment, current, replicas)
    correlation_id = current_correlation_id()

    plan = Plan(
        plan_id=str(uuid.uuid4()),
        action=SCALE_DEPLOYMENT,
        namespace=namespace,
        name=name,
        replicas_from=current,
        replicas_to=replicas,
        args_hash=scale_args_hash(namespace, name, replicas, override),
        resource_version=str(deployment.metadata.resource_version),
        generation=int(deployment.metadata.generation),
        uid=uid,
        created_at=now,
        expires_at=now + settings.plan_ttl_seconds,
        retain_until=now + retention_seconds(settings),
        requested_by=REQUESTER_GATEWAY,
        status=STATUS_PLANNED,
        override_manual_change=override,
        impact=impact,
        plan_correlation_id=correlation_id,
    )
    clients.store.create(plan)
    checks = (
        "arguments",
        "namespace_allowed",
        "replica_bounds",
        "not_gitops_managed",
        "no_hpa",
        "blast_radius",
        "no_manual_change",
        "cooldown",
        "not_flapping",
        "object_budget",
        "plan_rate",
        "global_budget",
    )
    preflight: list[dict[str, Any]] = [{"check": check, "passed": True} for check in checks]
    if overridden:
        preflight[checks.index("no_manual_change")]["overridden"] = True
    return ok(
        {
            "plan_id": plan.plan_id,
            "diff": {"replicas": {"from": plan.replicas_from, "to": plan.replicas_to}},
            "override_manual_change": override,
            "preflight": preflight,
            "impact": impact,
            "expires_at": plan.expires_at,
            "correlation_id": correlation_id,
        },
        truncated=impact["truncated"],
    )


def request_approval(
    args: dict[str, Any], settings: Settings, clients: Clients, now: int
) -> Envelope:
    """Ask the configured approvers to decide on a plan. Approves nothing; only the plan_id
    is read from the arguments, so no agent text reaches the approvers."""
    plan_id = require_plan_id(args.get("plan_id"))
    if not settings.approval_required:
        # A server approval would change nothing, and the wait would only keep the plan alive.
        raise GuardError(INVALID_ARGUMENT, "approval is handled by the client in client mode")
    if settings.approval_topic_arn is None:
        # Fail before the plan changes state: an approval nobody hears about cannot come.
        raise GuardError(INTERNAL, "no approval destination is configured")
    correlation_id = current_correlation_id()
    item, retry_after = clients.store.request_approval(
        plan_id,
        now,
        settings.approval_wait_ttl_seconds,
        settings.approval_request_cooldown_seconds,
        settings.approval_request_limit,
        correlation_id,
    )
    data: dict[str, Any] = {
        "plan_id": plan_id,
        "status": STATUS_AWAITING,
        "expires_at": plain(item.get("expires_at")),
        "notified": retry_after is None,
        "approval_mode": settings.approval_mode,
        "correlation_id": correlation_id,
    }
    if retry_after is not None:
        data["retry_after"] = retry_after
        return ok(data)
    clients.sns.publish(
        TopicArn=settings.approval_topic_arn,
        Subject=APPROVAL_SUBJECT,
        Message=notification(plan_view(item, now), settings.approval_url),
    )
    return ok(data)


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
    "request_approval": lambda args: request_approval(
        args, _settings(), Clients(_settings()), int(time.time())
    ),
}


def lambda_handler(event: Any, context: Any) -> Envelope:
    return dispatch(HANDLERS, event, context)
