"""Measured impact preview of a plan (mcp/CONTRACT.md, Impact preview).

Pure functions over objects the read handler has already read, as the API server returns
them (JSON objects with camelCase keys); nothing here calls the API. Values that depend on
what happens after the change are named `projected_*`.
"""

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

PDB_HEADROOM = "pdb_headroom"
SERVICE_WITHOUT_ENDPOINTS = "service_without_endpoints"
SCALE_TO_ZERO = "scale_to_zero"
RECREATE_STRATEGY = "recreate_strategy"
ROLLOUT_IN_PROGRESS = "rollout_in_progress"
NO_PODS = "no_pods"
REVERT_BASELINE_UNHEALTHY = "revert_baseline_unhealthy"
RECREATE = "Recreate"
ROLLING_UPDATE = "RollingUpdate"
# The API server's defaults for a RollingUpdate Deployment.
DEFAULT_MAX_UNAVAILABLE = "25%"
DEFAULT_MAX_SURGE = "25%"
_PERCENT = re.compile(r"^(\d+)%$")
SERVICE_NAME_LABEL = "kubernetes.io/service-name"
# Items returned per list, so a namespace with many matches cannot blow up the response.
# Warnings are computed over every match before the lists are cut.
LIST_LIMIT = 20

Obj = Mapping[str, Any]


def _get(obj: Obj | None, *path: str) -> Any:
    for key in path:
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(key)
    return obj


def _expression_matches(expression: Obj, labels: Mapping[str, str]) -> bool:
    key, operator = expression.get("key"), expression.get("operator")
    values = expression.get("values") or []
    if operator == "In":
        return key in labels and labels[key] in values
    if operator == "NotIn":
        return key not in labels or labels[key] not in values
    if operator == "Exists":
        return key in labels
    if operator == "DoesNotExist":
        return key not in labels
    # The API server rejects other operators; refuse rather than guess what one means.
    raise ValueError(f"unsupported label selector operator: {operator!r}")


def selector_matches(selector: Obj | None, labels: Mapping[str, str]) -> bool:
    """Label selector semantics of policy/v1: a null selector matches no pods, an empty
    selector matches every pod in the namespace."""
    if selector is None:
        return False
    for key, value in (selector.get("matchLabels") or {}).items():
        if labels.get(key) != value:
            return False
    return all(_expression_matches(e, labels) for e in selector.get("matchExpressions") or [])


def service_selects(service: Obj, labels: Mapping[str, str]) -> bool:
    """A Service without a selector (null or empty) has its endpoints managed by hand; skip it."""
    selector = _get(service, "spec", "selector")
    if not selector:
        return False
    return all(labels.get(key) == value for key, value in selector.items())


def ready_endpoints(slices: Iterable[Obj]) -> int:
    """Endpoints whose conditions.ready is true across the given EndpointSlices.

    An unset `ready` counts as not ready, so the count can be low but never high. A pod is
    counted once even when it appears in several slices, as in a dual-stack Service.
    """
    seen: set[tuple[str, ...]] = set()
    for endpoint_slice in slices:
        for endpoint in endpoint_slice.get("endpoints") or []:
            if _get(endpoint, "conditions", "ready") is not True:
                continue
            uid = _get(endpoint, "targetRef", "uid")
            if uid:
                seen.add(("uid", uid))
            else:
                seen.add(("addresses", *sorted(endpoint.get("addresses") or [])))
    return len(seen)


def slices_by_service(slices: Iterable[Obj]) -> dict[str, list[Obj]]:
    grouped: dict[str, list[Obj]] = {}
    for endpoint_slice in slices:
        service = (_get(endpoint_slice, "metadata", "labels") or {}).get(SERVICE_NAME_LABEL)
        if service:
            grouped.setdefault(service, []).append(endpoint_slice)
    return grouped


def _pdb_summary(pdb: Obj) -> dict[str, Any]:
    return {
        "name": _get(pdb, "metadata", "name"),
        "min_available": _get(pdb, "spec", "minAvailable"),
        "max_unavailable": _get(pdb, "spec", "maxUnavailable"),
        "current_healthy": _get(pdb, "status", "currentHealthy"),
        "desired_healthy": _get(pdb, "status", "desiredHealthy"),
        "disruptions_allowed": _get(pdb, "status", "disruptionsAllowed"),
    }


def revert_warnings(available_now: int, replicas: int) -> list[dict[str, str]]:
    """For a plan with revert_on_failure: the verification compares with the target, not
    with the state before the change, so a Deployment that is already short of available
    pods can be found degraded for a cause the change did not bring, and reverted."""
    if available_now >= replicas:
        return []
    return [
        {
            "code": REVERT_BASELINE_UNHEALTHY,
            "message": f"only {available_now} of {replicas} pods are available now; the "
            "verification may find the change degraded for a cause it did not bring, and the "
            "revert would then undo it",
        }
    ]


def scale_impact(
    labels: Mapping[str, str],
    ready_now: int,
    replicas_from: int,
    replicas_to: int,
    pdbs: Sequence[Obj],
    services: Sequence[Obj],
    slices: Sequence[Obj],
    action_warnings: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """Pods, PodDisruptionBudgets and Services affected by scaling a Deployment whose pod
    template has `labels` and which has `ready_now` ready pods, and warnings for the
    approver. `action_warnings` (revert_warnings) come first, so a cut never drops them.
    Warnings never block the plan."""
    removed = max(0, replicas_from - replicas_to)
    warnings: list[dict[str, str]] = [dict(w) for w in action_warnings]
    if replicas_to == 0:
        warnings.append({"code": SCALE_TO_ZERO, "message": "the Deployment would have no pods"})

    matching_pdbs = sorted(
        (_pdb_summary(p) for p in pdbs if selector_matches(_get(p, "spec", "selector"), labels)),
        key=lambda p: p["name"] or "",
    )
    for pdb in matching_pdbs:
        allowed = pdb["disruptions_allowed"] or 0
        if removed > allowed:
            warnings.append(
                {
                    "code": PDB_HEADROOM,
                    "message": f"removing {removed} pod(s) is more than PodDisruptionBudget "
                    f"{pdb['name']} allows now ({allowed}); a scale-down is not an eviction, "
                    "so the PodDisruptionBudget will not stop it",
                }
            )

    endpoints = slices_by_service(slices)
    matching_services = []
    for service in sorted(services, key=lambda s: _get(s, "metadata", "name") or ""):
        if not service_selects(service, labels):
            continue
        name = _get(service, "metadata", "name")
        now = ready_endpoints(endpoints.get(name, []))
        # Assumes the new pods become ready and the Service's other backends stay as they are.
        projected = max(0, now - ready_now) + replicas_to
        matching_services.append(
            {"name": name, "ready_endpoints_now": now, "projected_ready_endpoints": projected}
        )
        if projected == 0:
            warnings.append(
                {
                    "code": SERVICE_WITHOUT_ENDPOINTS,
                    "message": f"Service {name} is projected to have no ready endpoints",
                }
            )

    truncated = any(
        len(items) > LIST_LIMIT for items in (matching_pdbs, matching_services, warnings)
    )
    return {
        "pods": {
            "ready_now": ready_now,
            "from": replicas_from,
            "to": replicas_to,
            "added": max(0, replicas_to - replicas_from),
            "removed": removed,
        },
        "pod_disruption_budgets": matching_pdbs[:LIST_LIMIT],
        "services": matching_services[:LIST_LIMIT],
        "warnings": warnings[:LIST_LIMIT],
        "truncated": truncated,
    }


def scaled_value(value: Any, total: int, round_up: bool) -> int:
    """An int-or-percent field as a count of `total`, the way the Deployment controller
    resolves it (k8s.io/apimachinery intstr.GetScaledValueFromIntOrPercent)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    match = _PERCENT.match(value) if isinstance(value, str) else None
    if match is None:
        raise ValueError(f"invalid int or percent value: {value!r}")
    scaled = int(match.group(1)) * total / 100
    return math.ceil(scaled) if round_up else math.floor(scaled)


def rollout_strategy(deployment: Obj, replicas: int) -> dict[str, Any]:
    """The Deployment's strategy with maxUnavailable and maxSurge resolved to pod counts, as
    the controller resolves them (deployment util ResolveFenceposts): maxUnavailable rounds
    down, maxSurge rounds up, and if both are 0 maxUnavailable becomes 1. Recreate has
    neither."""
    strategy = _get(deployment, "spec", "strategy") or {}
    if strategy.get("type") == RECREATE:
        return {"strategy": RECREATE, "max_unavailable": None, "max_surge": None}
    rolling = strategy.get("rollingUpdate") or {}
    surge = scaled_value(rolling.get("maxSurge", DEFAULT_MAX_SURGE), replicas, round_up=True)
    unavailable = scaled_value(
        rolling.get("maxUnavailable", DEFAULT_MAX_UNAVAILABLE), replicas, round_up=False
    )
    if surge == 0 and unavailable == 0:
        unavailable = 1
    return {"strategy": ROLLING_UPDATE, "max_unavailable": unavailable, "max_surge": surge}


def rollout_in_progress(deployment: Obj, replicas: int) -> bool:
    generation = _get(deployment, "metadata", "generation") or 0
    observed = _get(deployment, "status", "observedGeneration") or 0
    updated = _get(deployment, "status", "updatedReplicas") or 0
    return observed < generation or updated < replicas


def rollout_impact(
    deployment: Obj,
    labels: Mapping[str, str],
    ready_now: int,
    replicas: int,
    pdbs: Sequence[Obj],
    services: Sequence[Obj],
    slices: Sequence[Obj],
    action_warnings: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """Pods, PodDisruptionBudgets and Services affected by rolling out a new pod template to
    a Deployment with `replicas` desired pods, and warnings for the approver. Every pod is
    replaced at the pace of the Deployment's own strategy. `action_warnings` (a rollback's)
    come first, so a cut never drops them. Warnings never block the plan."""
    rollout = rollout_strategy(deployment, replicas)
    recreate = rollout["strategy"] == RECREATE
    # Pods below the desired count at the lowest point of the rollout.
    down = replicas if recreate else min(rollout["max_unavailable"], replicas)
    warnings: list[dict[str, str]] = [dict(w) for w in action_warnings]
    if recreate:
        warnings.append(
            {
                "code": RECREATE_STRATEGY,
                "message": "the Deployment uses the Recreate strategy: every pod is stopped "
                "before new ones start",
            }
        )

    matching_pdbs = sorted(
        (_pdb_summary(p) for p in pdbs if selector_matches(_get(p, "spec", "selector"), labels)),
        key=lambda p: p["name"] or "",
    )
    for pdb in matching_pdbs:
        allowed = pdb["disruptions_allowed"] or 0
        if down > allowed:
            warnings.append(
                {
                    "code": PDB_HEADROOM,
                    "message": f"the rollout can take {down} pod(s) down at once, more than "
                    f"PodDisruptionBudget {pdb['name']} allows now ({allowed}); a rollout is "
                    "not an eviction, so the PodDisruptionBudget will not stop it",
                }
            )

    endpoints = slices_by_service(slices)
    matching_services = []
    for service in sorted(services, key=lambda s: _get(s, "metadata", "name") or ""):
        if not service_selects(service, labels):
            continue
        name = _get(service, "metadata", "name")
        now = ready_endpoints(endpoints.get(name, []))
        # The lowest point of the rollout, assuming new pods become ready and the Service's
        # other backends stay as they are. It includes the start, so this Deployment's ready
        # pods count no more than are ready now.
        projected = max(0, now - ready_now) + min(ready_now, replicas - down)
        matching_services.append(
            {"name": name, "ready_endpoints_now": now, "projected_min_ready_endpoints": projected}
        )
        if projected == 0:
            warnings.append(
                {
                    "code": SERVICE_WITHOUT_ENDPOINTS,
                    "message": f"Service {name} is projected to have no ready endpoints "
                    "during the rollout",
                }
            )

    if rollout_in_progress(deployment, replicas):
        warnings.append(
            {
                "code": ROLLOUT_IN_PROGRESS,
                "message": "a rollout of the Deployment has not finished yet",
            }
        )
    if replicas == 0:
        warnings.append(
            {
                "code": NO_PODS,
                "message": "the Deployment has no pods; the template changes but no pod restarts",
            }
        )

    truncated = any(
        len(items) > LIST_LIMIT for items in (matching_pdbs, matching_services, warnings)
    )
    return {
        "pods": {
            "ready_now": ready_now,
            "from": replicas,
            "to": replicas,
            "added": 0,
            "removed": 0,
            "replaced": replicas,
        },
        "rollout": rollout,
        "pod_disruption_budgets": matching_pdbs[:LIST_LIMIT],
        "services": matching_services[:LIST_LIMIT],
        "warnings": warnings[:LIST_LIMIT],
        "truncated": truncated,
    }
