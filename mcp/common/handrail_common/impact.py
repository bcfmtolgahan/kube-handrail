"""Measured impact preview of a scale plan (mcp/CONTRACT.md, Impact preview).

Pure functions over objects the read handler has already read, as the API server returns
them (JSON objects with camelCase keys); nothing here calls the API. Values that depend on
what happens after the change are named `projected_*`.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

PDB_HEADROOM = "pdb_headroom"
SERVICE_WITHOUT_ENDPOINTS = "service_without_endpoints"
SCALE_TO_ZERO = "scale_to_zero"
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


def scale_impact(
    labels: Mapping[str, str],
    ready_now: int,
    replicas_from: int,
    replicas_to: int,
    pdbs: Sequence[Obj],
    services: Sequence[Obj],
    slices: Sequence[Obj],
) -> dict[str, Any]:
    """Pods, PodDisruptionBudgets and Services affected by scaling a Deployment whose pod
    template has `labels` and which has `ready_now` ready pods, and warnings for the
    approver. Warnings never block the plan."""
    removed = max(0, replicas_from - replicas_to)
    warnings: list[dict[str, str]] = []
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
