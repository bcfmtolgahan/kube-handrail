"""k8s_read Lambda: Deployment status and events, and change planning. Never mutates the cluster."""

import json
import re
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
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
from handrail_common.impact import rollout_impact, scale_impact
from handrail_common.observability import current_correlation_id
from handrail_common.plans import (
    GLOBAL_BUDGET_WINDOW_SECONDS,
    REQUESTER_GATEWAY,
    RESTART_DEPLOYMENT,
    SCALE_DEPLOYMENT,
    STATUS_AWAITING,
    STATUS_PLANNED,
    Plan,
    PlanStore,
    global_budget_error,
    object_key,
    plan_args_hash,
    restart_args,
    restart_template,
    scale_args,
    template_hash,
)
from handrail_common.untrusted import fit_list, untrusted_text
from handrail_common.validation import (
    GuardError,
    check_namespace_allowed,
    check_not_paused,
    check_replica_step,
    require_int,
    require_name,
    require_namespace,
    require_override_flag,
    require_plan_id,
    validate_scale_args,
    validate_target_args,
)

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
APPROVAL_SUBJECT = "kube-handrail: approval requested"
# The API server defaults spec.replicas to 1 when it is omitted.
DEFAULT_REPLICAS = 1
DEFAULT_EVENT_LIMIT = 20
MAX_EVENT_LIMIT = 50
# Generated names: the API server cuts generateName to 58 characters and appends 5 random
# lower-case letters or digits (k8s.io/apiserver names.SimpleNameGenerator).
GENERATED_PREFIX_MAX = 58
_GENERATED_SUFFIX = re.compile(r"^[a-z0-9]{5}$")
_TIMESTAMP = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$"
)


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


@cache
def _models() -> k8s.ApiClient:
    # Only deserializes; never sends a request.
    return k8s.ApiClient()


def _read_deployment_raw(
    apps: k8s.AppsV1Api, namespace: str, name: str
) -> tuple[Any, dict[str, Any]]:
    """The Deployment as a client model and as the raw JSON the API server returned, from
    one read. Hashes are taken over the raw JSON: the models drop fields the client does not
    know."""
    try:
        response = apps.read_namespaced_deployment(
            name=name, namespace=namespace, _preload_content=False
        )
    except ApiException as exc:
        raise _kubernetes_error(exc, f"deployment {namespace}/{name}") from None
    return _models().deserialize(response, "V1Deployment"), json.loads(response.data)


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


# Builds the impact preview from the pod template labels, the ready pods and the
# PodDisruptionBudgets, Services and EndpointSlices of the namespace.
Preview = Callable[
    [Mapping[str, str], int, Sequence[Any], Sequence[Any], Sequence[Any]], dict[str, Any]
]


def _impact(
    clients: "Clients", namespace: str, deployment: Any, preview: Preview
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
        return preview(_pod_template_labels(deployment), ready_now, pdbs, services, slices)
    except ValueError as exc:
        # An object the preview cannot interpret, such as an unknown selector operator.
        raise GuardError(KUBERNETES_ERROR, f"cannot build the impact preview: {exc}") from None


def _revision(deployment: Any) -> int | None:
    """The Deployment's revision annotation as an integer, or None when it is not one."""
    value = (deployment.metadata.annotations or {}).get(REVISION_ANNOTATION)
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _desired_replicas(deployment: Any) -> int:
    replicas = deployment.spec.replicas if deployment.spec else None
    return DEFAULT_REPLICAS if replicas is None else replicas


def _conditions(status: Any) -> tuple[list[dict[str, Any]], bool]:
    """Conditions with reason and message as untrusted text, and whether any was cut."""
    conditions = []
    truncated = False
    for c in (status.conditions if status else None) or []:
        reason, reason_cut = untrusted_text(c.reason)
        message, message_cut = untrusted_text(c.message)
        truncated = truncated or reason_cut or message_cut
        conditions.append(
            {"type": c.type, "status": c.status, "reason": reason, "message": message}
        )
    return conditions, truncated


def _summary(deployment: Any) -> tuple[dict[str, Any], bool]:
    meta = deployment.metadata
    status = deployment.status
    conditions, truncated = _conditions(status)
    summary = {
        "namespace": meta.namespace,
        "name": meta.name,
        "replicas": {
            "desired": _desired_replicas(deployment),
            "ready": (status.ready_replicas if status else None) or 0,
            "updated": (status.updated_replicas if status else None) or 0,
            "available": (status.available_replicas if status else None) or 0,
        },
        "conditions": conditions,
        "revision": (meta.annotations or {}).get(REVISION_ANNOTATION),
        "resource_version": meta.resource_version,
        "gitops_managed": is_gitops_managed(deployment),
    }
    return summary, truncated


def _owned_replica_sets(replica_sets: list[dict[str, Any]], owner_uid: str) -> dict[str, str]:
    """Name => uid of the ReplicaSets with an owner reference to `owner_uid`."""
    owned = {}
    for rs in replica_sets:
        meta = rs.get("metadata") or {}
        owners = meta.get("ownerReferences") or []
        if isinstance(meta.get("name"), str) and any(
            isinstance(o, Mapping) and o.get("uid") == owner_uid for o in owners
        ):
            owned[meta["name"]] = str(meta.get("uid") or "")
    return owned


def _is_generated_from(name: str, base: str) -> bool:
    prefix = f"{base}-"[:GENERATED_PREFIX_MAX]
    return name.startswith(prefix) and bool(_GENERATED_SUFFIX.match(name[len(prefix) :]))


def _is_name(value: Any) -> bool:
    try:
        require_name(value)
    except GuardError:
        return False
    return True


def _involved(
    event: dict[str, Any],
    namespace: str,
    deployment: tuple[str, str],
    replica_sets: Mapping[str, str],
) -> dict[str, str] | None:
    """The matched object {kind, name}, or None when the event is about something else.
    Everything in involvedObject is set by whoever wrote the event, so it must match
    names kube-handrail derived itself."""
    involved = event.get("involvedObject") or {}
    kind, name, uid = involved.get("kind"), involved.get("name"), involved.get("uid")
    if involved.get("namespace") not in (None, namespace) or not _is_name(name):
        return None
    uid_unset = uid in (None, "")
    if kind == "Deployment" and name == deployment[0] and (uid_unset or uid == deployment[1]):
        return {"kind": kind, "name": name}
    if kind == "ReplicaSet" and name in replica_sets and (uid_unset or uid == replica_sets[name]):
        return {"kind": kind, "name": name}
    if kind == "Pod" and any(_is_generated_from(name, rs) for rs in replica_sets):
        return {"kind": kind, "name": name}
    return None


def _timestamp(value: Any) -> tuple[float, str] | None:
    """(epoch seconds, the original string) for an RFC 3339 timestamp, else None."""
    match = _TIMESTAMP.match(value) if isinstance(value, str) else None
    if match is None:
        return None
    seconds, fraction, zone = match.groups()
    # fromisoformat takes at most 6 fractional digits; MicroTime has 6, but be lenient.
    text = seconds + (f".{fraction[:6]}" if fraction else "") + ("+00:00" if zone == "Z" else zone)
    try:
        return datetime.fromisoformat(text).timestamp(), value
    except ValueError:
        return None


def _first_timestamp(*values: Any) -> tuple[float, str] | None:
    for value in values:
        parsed = _timestamp(value)
        if parsed is not None:
            return parsed
    return None


def _count(event: dict[str, Any]) -> int:
    for value in (event.get("count"), (event.get("series") or {}).get("count")):
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return 1


def _sort_timestamp(event: dict[str, Any]) -> tuple[float, str] | None:
    return _first_timestamp(
        event.get("lastTimestamp"),
        (event.get("series") or {}).get("lastObservedTime"),
        event.get("eventTime"),
        event.get("firstTimestamp"),
        (event.get("metadata") or {}).get("creationTimestamp"),
    )


def _event_view(event: dict[str, Any], obj: dict[str, str]) -> tuple[dict[str, Any], bool]:
    type_, type_cut = untrusted_text(event.get("type"))
    reason, reason_cut = untrusted_text(event.get("reason"))
    message, message_cut = untrusted_text(event.get("message"))
    first = _first_timestamp(event.get("firstTimestamp"), event.get("eventTime"))
    last = _sort_timestamp(event)
    view = {
        "type": type_,
        "reason": reason,
        "message": message,
        "object": obj,
        "count": _count(event),
        "first_timestamp": first[1] if first else None,
        "last_timestamp": last[1] if last else None,
    }
    return view, type_cut or reason_cut or message_cut


def _newest_first(event: dict[str, Any]) -> tuple[bool, float, str]:
    stamp = _sort_timestamp(event)
    name = str((event.get("metadata") or {}).get("name") or "")
    return (stamp is None, -stamp[0] if stamp else 0.0, name)


def _event_limit(args: Mapping[str, Any]) -> int:
    value = args.get("limit")
    if value is None:
        return DEFAULT_EVENT_LIMIT
    limit = require_int(value, "limit")
    if not 1 <= limit <= MAX_EVENT_LIMIT:
        raise GuardError(INVALID_ARGUMENT, f"limit must be between 1 and {MAX_EVENT_LIMIT}")
    return limit


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
    summary, truncated = _summary(_read_deployment(clients.apps, namespace, name))
    return ok(summary, truncated=truncated)


def get_workload_events(args: dict[str, Any], settings: Settings, clients: Clients) -> Envelope:
    """Events about a Deployment, its ReplicaSets and their Pods, newest first, as
    untrusted text. Pods are matched by name; the reader cannot read them."""
    namespace = require_namespace(args.get("namespace"))
    name = require_name(args.get("name"))
    limit = _event_limit(args)
    check_namespace_allowed(namespace, settings.allowed_namespaces)
    deployment = _read_deployment(clients.apps, namespace, name)
    uid = str(deployment.metadata.uid)
    replica_sets = _owned_replica_sets(
        _list(
            "ReplicaSets", partial(clients.apps.list_namespaced_replica_set, namespace=namespace)
        ),
        uid,
    )
    events = _list("Events", partial(clients.core.list_namespaced_event, namespace=namespace))
    matched = []
    for event in events:
        obj = _involved(event, namespace, (name, uid), replica_sets)
        if obj is not None:
            matched.append((event, obj))
    matched.sort(key=lambda pair: _newest_first(pair[0]))
    views = [_event_view(event, obj) for event, obj in matched[:limit]]
    data, dropped = fit_list(
        {
            "namespace": namespace,
            "name": name,
            "events": [view for view, _ in views],
            "matched": len(matched),
            "truncated": False,
        },
        "events",
    )
    data["truncated"] = dropped
    field_cut = any(cut for _, cut in views[: len(data["events"])])
    return ok(data, truncated=dropped or field_cut)


@dataclass(frozen=True)
class PlanAction:
    """What one action adds to the generic plan path (_plan)."""

    # The plan record's `action`.
    name: str
    # Argument checks 1-3: (namespace, name, args), where args is the argument payload the
    # plan stores and args_hash covers: the apply arguments without plan_id, optional ones
    # only when given.
    validate: Callable[[Mapping[str, Any], Settings], tuple[str, str, dict[str, Any]]]
    # The resulting replica count, from the current one and the arguments.
    replicas_to: Callable[[int, Mapping[str, Any]], int]
    # Checks on the live Deployment after the GitOps check (scale: HPA and blast radius).
    checks: Callable[[Clients, Settings, Any, int, Mapping[str, Any]], None]
    # History checks; returns whether the manual-change override was used.
    breaker: Callable[[Clients, Settings, Any, int, Mapping[str, Any], int], bool]
    # Names of the checks reported in preflight, in order.
    preflight: tuple[str, ...]
    diff: Callable[[Plan], dict[str, Any]]
    # The pod template the change leaves, from the plan id and the current raw template.
    template_to: Callable[[str, Mapping[str, Any]], dict[str, Any]]
    # The impact preview, given the raw Deployment, the current and the resulting replicas.
    impact: Callable[[Mapping[str, Any], int, int], Preview]


def _plan(
    action: PlanAction, args: Mapping[str, Any], settings: Settings, clients: Clients, now: int
) -> Envelope:
    namespace, name, request = action.validate(args, settings)
    override = request.get("override_manual_change", False) is True
    deployment, raw = _read_deployment_raw(clients.apps, namespace, name)
    if is_gitops_managed(deployment):
        raise GuardError(GITOPS_MANAGED, "deployment is managed by GitOps; change it in Git")
    current = _desired_replicas(deployment)
    action.checks(clients, settings, deployment, current, request)
    # History checks give early feedback; apply runs all but the plan rate limit again.
    overridden = action.breaker(clients, settings, deployment, current, request, now)
    # Read only here; apply takes a unit of the budget atomically.
    if clients.store.global_applies(now) >= settings.global_hourly_apply_budget:
        raise global_budget_error(
            now - now % GLOBAL_BUDGET_WINDOW_SECONDS, settings.global_hourly_apply_budget
        )
    replicas_to = action.replicas_to(current, request)
    # Last, after every check has passed, and before the plan is stored.
    impact = _impact(clients, namespace, deployment, action.impact(raw, current, replicas_to))
    correlation_id = current_correlation_id()
    plan_id = str(uuid.uuid4())
    template = (raw.get("spec") or {}).get("template") or {}
    template_from = template_hash(template)
    template_to = template_hash(action.template_to(plan_id, template))

    plan = Plan(
        plan_id=plan_id,
        action=action.name,
        namespace=namespace,
        name=name,
        replicas_from=current,
        replicas_to=replicas_to,
        args_hash=plan_args_hash(action.name, request),
        resource_version=str(deployment.metadata.resource_version),
        generation=int(deployment.metadata.generation),
        uid=str(deployment.metadata.uid),
        created_at=now,
        expires_at=now + settings.plan_ttl_seconds,
        retain_until=now + retention_seconds(settings),
        requested_by=REQUESTER_GATEWAY,
        status=STATUS_PLANNED,
        override_manual_change=override,
        impact=impact,
        plan_correlation_id=correlation_id,
        args=request,
        template_from=template_from,
        template_to=template_to,
        revision_from=_revision(deployment) if template_to != template_from else None,
    )
    clients.store.create(plan)
    preflight: list[dict[str, Any]] = [
        {"check": check, "passed": True} for check in action.preflight
    ]
    if overridden:
        preflight[action.preflight.index("no_manual_change")]["overridden"] = True
    return ok(
        {
            "plan_id": plan.plan_id,
            "diff": action.diff(plan),
            "override_manual_change": override,
            "preflight": preflight,
            "impact": impact,
            "expires_at": plan.expires_at,
            "correlation_id": correlation_id,
        },
        truncated=impact["truncated"],
    )


# --- scale_deployment ---------------------------------------------------------


def _validate_scale(args: Mapping[str, Any], settings: Settings) -> tuple[str, str, dict[str, Any]]:
    namespace, name, replicas = validate_scale_args(
        args, settings.allowed_namespaces, settings.bounds_for
    )
    override = require_override_flag(args)
    return namespace, name, scale_args(namespace, name, replicas, override)


def _check_scale(
    clients: Clients, settings: Settings, deployment: Any, current: int, args: Mapping[str, Any]
) -> None:
    hpas = _targeting_hpas(clients.autoscaling, args["namespace"], args["name"])
    if hpas:
        raise GuardError(
            CONFLICT, f"HorizontalPodAutoscaler {', '.join(sorted(hpas))} targets this deployment"
        )
    check_replica_step(current, args["replicas"], settings.max_replica_step_percent)


def _scale_breaker(
    clients: Clients,
    settings: Settings,
    deployment: Any,
    current: int,
    args: Mapping[str, Any],
    now: int,
) -> bool:
    last_applied = last_applied_plan(clients.store, deployment)
    overridden = check_manual_change(
        last_applied, current, args.get("override_manual_change", False) is True
    )
    check_history(
        clients.store,
        object_key(args["namespace"], args["name"], str(deployment.metadata.uid)),
        now,
        settings,
        current,
        args["replicas"],
        plan_rate=True,
        last_applied=last_applied,
    )
    return overridden


SCALE = PlanAction(
    name=SCALE_DEPLOYMENT,
    validate=_validate_scale,
    replicas_to=lambda current, args: args["replicas"],
    checks=_check_scale,
    breaker=_scale_breaker,
    preflight=(
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
    ),
    diff=lambda plan: {"replicas": {"from": plan.replicas_from, "to": plan.replicas_to}},
    # A scale leaves the pod template as it is.
    template_to=lambda plan_id, template: dict(template),
    impact=lambda raw, current, replicas_to: (
        lambda labels, ready, pdbs, services, slices: scale_impact(
            labels, ready, current, replicas_to, pdbs, services, slices
        )
    ),
)


def plan_scale_deployment(
    args: dict[str, Any], settings: Settings, clients: Clients, now: int
) -> Envelope:
    return _plan(SCALE, args, settings, clients, now)


# --- restart_deployment -------------------------------------------------------


def _validate_restart(
    args: Mapping[str, Any], settings: Settings
) -> tuple[str, str, dict[str, Any]]:
    namespace, name = validate_target_args(args, settings.allowed_namespaces)
    return namespace, name, restart_args(namespace, name)


def _check_restart(
    clients: Clients, settings: Settings, deployment: Any, current: int, args: Mapping[str, Any]
) -> None:
    # No HPA check: a restart leaves spec.replicas, which an HPA owns, as it is.
    check_not_paused(deployment)


def _restart_breaker(
    clients: Clients,
    settings: Settings,
    deployment: Any,
    current: int,
    args: Mapping[str, Any],
    now: int,
) -> bool:
    # No manual-change check: a restart undoes no change. Replicas stay as they are, so the
    # change has no direction and is never flapping.
    check_history(
        clients.store,
        object_key(args["namespace"], args["name"], str(deployment.metadata.uid)),
        now,
        settings,
        current,
        current,
        plan_rate=True,
        last_applied=last_applied_plan(clients.store, deployment),
    )
    return False


RESTART = PlanAction(
    name=RESTART_DEPLOYMENT,
    validate=_validate_restart,
    replicas_to=lambda current, args: current,
    checks=_check_restart,
    breaker=_restart_breaker,
    preflight=(
        "arguments",
        "namespace_allowed",
        "not_gitops_managed",
        "not_paused",
        "cooldown",
        "object_budget",
        "plan_rate",
        "global_budget",
    ),
    diff=lambda plan: {
        "restart": {"pods_replaced": plan.replicas_from},
        "template": {"from": plan.template_from, "to": plan.template_to},
    },
    template_to=lambda plan_id, template: restart_template(template, plan_id),
    impact=lambda raw, current, replicas_to: (
        lambda labels, ready, pdbs, services, slices: rollout_impact(
            raw, labels, ready, current, pdbs, services, slices
        )
    ),
)


def plan_restart_deployment(
    args: dict[str, Any], settings: Settings, clients: Clients, now: int
) -> Envelope:
    return _plan(RESTART, args, settings, clients, now)


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
    "get_workload_events": lambda args: get_workload_events(
        args, _settings(), Clients(_settings())
    ),
    "plan_scale_deployment": lambda args: plan_scale_deployment(
        args, _settings(), Clients(_settings()), int(time.time())
    ),
    "plan_restart_deployment": lambda args: plan_restart_deployment(
        args, _settings(), Clients(_settings()), int(time.time())
    ),
    "request_approval": lambda args: request_approval(
        args, _settings(), Clients(_settings()), int(time.time())
    ),
}


def lambda_handler(event: Any, context: Any) -> Envelope:
    return dispatch(HANDLERS, event, context)
