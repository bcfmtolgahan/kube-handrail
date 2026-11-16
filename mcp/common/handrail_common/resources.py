"""Operations per kind of object a plan changes (mcp/CONTRACT.md, Adjust HPA, and
mcp/contract/adjust-hpa.md).

A plan record's `target_kind` selects them: absent (every plan made before HPA adjust) or
"Deployment" for scale, restart and rollback, "HorizontalPodAutoscaler" for adjust_hpa.
ResourceOps reads, patches (the server-side dry run first), checks that the object is the
one planned with an unchanged spec, and evaluates it for the verification. `rt` is the
write Lambda's runtime: `apps()` and `autoscaling()` return its Kubernetes API clients.

An HPA has no metadata.generation and no status.observedGeneration (checked on kind,
Kubernetes 1.31), and the HPA controller writes its status every sync, which changes
resourceVersion without a spec change. So an HPA plan records the hash of the HPA's spec
(spec_from, spec_to) and compares that; resourceVersion is only the patch's precondition.
"""

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from handrail_common import verify
from handrail_common.breaker import LAST_PLAN_ANNOTATION
from handrail_common.envelope import CONFLICT, INVALID_ARGUMENT, OUT_OF_BOUNDS
from handrail_common.plans import Plan, template_hash
from handrail_common.validation import GuardError

DEPLOYMENT = "Deployment"
HORIZONTAL_POD_AUTOSCALER = "HorizontalPodAutoscaler"
# The admission policies whose denials the write Lambda maps to admission_denied.
DEPLOYMENT_POLICY = "kube-handrail-writer-bounds"
HPA_POLICY = "kube-handrail-writer-hpa-bounds"
# The API server defaults spec.replicas of a Deployment and minReplicas of an HPA to 1.
DEFAULT_REPLICAS = 1
DEFAULT_HPA_MIN = 1
# A plan record without target_kind is a Deployment plan (generation is then compared).
NO_GENERATION = -1
# verification.reason of an HPA adjust that the Deployment checks do not have: the HPA
# controller cannot get or update its target's scale (AbleToScale False).
NOT_ABLE_TO_SCALE = "not_able_to_scale"


def kind_of(plan: Plan) -> str:
    return plan.target_kind or DEPLOYMENT


# --- HorizontalPodAutoscaler spec, as the API server returns it (raw JSON) ---------------


def hpa_spec(hpa: Mapping[str, Any]) -> dict[str, Any]:
    spec = hpa.get("spec")
    return dict(spec) if isinstance(spec, Mapping) else {}


def hpa_bounds(spec: Mapping[str, Any]) -> tuple[int, int]:
    """(minReplicas, maxReplicas); minReplicas defaults to 1 when absent."""
    minimum = spec.get("minReplicas")
    return (DEFAULT_HPA_MIN if minimum is None else int(minimum), int(spec.get("maxReplicas", 0)))


def with_bounds(spec: Mapping[str, Any], minimum: int, maximum: int) -> dict[str, Any]:
    """The spec an adjust leaves: a copy with only minReplicas and maxReplicas changed."""
    changed = copy.deepcopy(dict(spec))
    changed["minReplicas"] = minimum
    changed["maxReplicas"] = maximum
    return changed


def spec_hash(spec: Mapping[str, Any]) -> str:
    """Hash of an HPA's whole spec (scaleTargetRef, bounds, metrics, behavior)."""
    return template_hash(spec)


def clamp(value: int, minimum: int, maximum: int) -> int:
    """What the HPA controller does to the pod count first, with or without metrics."""
    return max(minimum, min(value, maximum))


def scale_target(spec: Mapping[str, Any]) -> str:
    """The name of the Deployment the HPA scales; any other target is refused, since the
    tools read and verify Deployments only."""
    ref = spec.get("scaleTargetRef")
    ref = ref if isinstance(ref, Mapping) else {}
    api_version = ref.get("apiVersion")
    name = ref.get("name")
    if (
        ref.get("kind") != DEPLOYMENT
        or not isinstance(api_version, str)
        or not api_version.startswith("apps/")
        or not isinstance(name, str)
        or not name
    ):
        raise GuardError(
            INVALID_ARGUMENT,
            "the HorizontalPodAutoscaler does not scale a Deployment (apps); only "
            "Deployment targets are supported",
        )
    return name


def hpa_conditions(hpa: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    status = hpa.get("status")
    conditions = status.get("conditions") if isinstance(status, Mapping) else None
    return [c for c in conditions or [] if isinstance(c, Mapping)]


def _status_int(hpa: Mapping[str, Any], key: str) -> int | None:
    status = hpa.get("status")
    value = status.get(key) if isinstance(status, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def hpa_metadata(hpa: Mapping[str, Any]) -> Mapping[str, Any]:
    meta = hpa.get("metadata")
    return meta if isinstance(meta, Mapping) else {}


def hpa_annotations(hpa: Mapping[str, Any]) -> Mapping[str, Any]:
    annotations = hpa_metadata(hpa).get("annotations")
    return annotations if isinstance(annotations, Mapping) else {}


def resolve_bounds(
    args: Mapping[str, Any], current: tuple[int, int], bounds: tuple[int, int]
) -> tuple[int, int]:
    """(min_to, max_to) for an adjust: each given bound, else the current one. Both must lie
    within the namespace's bounds, since the admission policy checks both on any change;
    minReplicas at least 1 and at most maxReplicas; something must change; and the two
    bounds must not move in opposite directions (the breaker needs one direction: change
    them in two plans)."""
    min_to = args.get("min_replicas", current[0])
    max_to = args.get("max_replicas", current[1])
    if min_to < DEFAULT_HPA_MIN:
        raise GuardError(INVALID_ARGUMENT, "min_replicas must be at least 1")
    if min_to > max_to:
        raise GuardError(
            INVALID_ARGUMENT,
            f"min_replicas {min_to} would be above max_replicas {max_to}",
            {"min_replicas": min_to, "max_replicas": max_to},
        )
    minimum, maximum = bounds
    for label, value in (("min_replicas", min_to), ("max_replicas", max_to)):
        if not minimum <= value <= maximum:
            raise GuardError(
                OUT_OF_BOUNDS,
                f"{label} {value} is outside the bounds {minimum} to {maximum}",
                {label: value, "bounds": {"min_replicas": minimum, "max_replicas": maximum}},
            )
    if (min_to, max_to) == tuple(current):
        raise GuardError(INVALID_ARGUMENT, "the HorizontalPodAutoscaler already has these bounds")
    if (min_to - current[0]) * (max_to - current[1]) < 0:
        raise GuardError(
            INVALID_ARGUMENT,
            "min_replicas and max_replicas would move in opposite directions; change them "
            "in two plans",
        )
    return min_to, max_to


# --- Verification of an HPA adjust -----------------------------------------------------


def _deployment_condition(deployment: Any, kind: str) -> Any:
    for condition in getattr(deployment.status, "conditions", None) or []:
        if condition.type == kind:
            return condition
    return None


def evaluate_hpa(plan: Plan, hpa: Mapping[str, Any], target: Any) -> tuple[str, str]:
    """(outcome, reason) of one reading of the HPA (raw JSON) and the Deployment it scales
    (a client model, or None when it is gone). The first matching row wins, every unknown
    before any degraded, as for a Deployment (handrail_common.verify)."""
    meta = hpa_metadata(hpa)
    if str(meta.get("uid")) != plan.uid:
        return verify.UNKNOWN, verify.REPLACED
    if hpa_annotations(hpa).get(LAST_PLAN_ANNOTATION) != plan.plan_id:
        return verify.UNKNOWN, verify.SUPERSEDED
    spec = hpa_spec(hpa)
    if spec_hash(spec) != plan.spec_to or hpa_bounds(spec) != (plan.min_to, plan.max_to):
        return verify.UNKNOWN, verify.SPEC_CHANGED
    if target is None:
        return verify.UNKNOWN, verify.NOT_FOUND
    if getattr(target.spec, "paused", None) is True:
        return verify.UNKNOWN, verify.PAUSED
    conditions = {str(c.get("type")): c for c in hpa_conditions(hpa)}
    able = conditions.get("AbleToScale")
    if able is not None and able.get("status") == "False":
        return verify.DEGRADED, NOT_ABLE_TO_SCALE
    progressing = _deployment_condition(target, "Progressing")
    if (
        progressing is not None
        and progressing.status == "False"
        and progressing.reason == "ProgressDeadlineExceeded"
    ):
        return verify.DEGRADED, verify.PROGRESS_DEADLINE_EXCEEDED
    failure = _deployment_condition(target, "ReplicaFailure")
    if failure is not None and failure.status == "True":
        return verify.DEGRADED, verify.REPLICA_FAILURE
    current = _status_int(hpa, "currentReplicas")
    desired = DEFAULT_REPLICAS if target.spec.replicas is None else target.spec.replicas
    available = getattr(target.status, "available_replicas", None) or 0
    in_bounds = current is not None and plan.min_to <= current <= plan.max_to
    if not in_bounds or not plan.min_to <= desired <= plan.max_to or available < desired:
        return verify.DEGRADED, verify.TARGET_NOT_REACHED
    return verify.HEALTHY, verify.TARGET_REACHED


def observed_hpa(hpa: Mapping[str, Any], target: Any) -> dict[str, Any]:
    """The HPA's bounds, counts and conditions (type, status, reason; never the message)
    and the target's counts the outcome was decided on."""
    minimum, maximum = hpa_bounds(hpa_spec(hpa))
    view: dict[str, Any] = {
        "min_replicas": minimum,
        "max_replicas": maximum,
        "current_replicas": _status_int(hpa, "currentReplicas"),
        "desired_replicas": _status_int(hpa, "desiredReplicas"),
        "conditions": [
            {
                "type": str(c.get("type")),
                "status": str(c.get("status")),
                "reason": None
                if c.get("reason") is None
                else str(c.get("reason"))[: verify.REASON_MAX_CHARS],
            }
            for c in hpa_conditions(hpa)
        ],
    }
    if target is not None:
        view["target"] = verify.observed(target)
    return view


# --- The operations --------------------------------------------------------------------


@dataclass(frozen=True)
class ResourceOps:
    """What the write Lambda does with one kind of object (mcp/contract/adjust-hpa.md)."""

    kind: str
    # The admission policy that bounds the writer's changes to this kind.
    policy: str
    # (rt, namespace, name) => the object; an ApiException propagates.
    read: Callable[[Any, str, str], Any]
    # (rt, namespace, name, body, dry_run) => the patched object; an ApiException propagates.
    patch: Callable[[Any, str, str, Any, bool], Any]
    uid: Callable[[Any], str]
    resource_version: Callable[[Any], str]
    annotations: Callable[[Any], Mapping[str, Any]]
    # Raise conflict unless the object's spec is still the one the plan was made on
    # (Deployment: generation and replicas; HPA: the spec hash and the bounds).
    check_unchanged: Callable[[Plan, Any], None]
    # (rt, plan, object) => (outcome, reason, observed) of the verification.
    evaluate: Callable[[Any, Plan, Any], tuple[str, str, dict[str, Any]]]


def _deployment_unchanged(plan: Plan, deployment: Any) -> None:
    """The API server bumps a Deployment's generation on spec and annotation changes (the
    apply itself bumps it once, by setting the plan annotation), but not on status or label
    changes."""
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


def _patch_deployment(rt: Any, namespace: str, name: str, body: Any, dry_run: bool) -> Any:
    apps = rt.apps()
    if dry_run:
        return apps.patch_namespaced_deployment(name, namespace, body, dry_run="All")
    return apps.patch_namespaced_deployment(name, namespace, body)


def _evaluate_deployment(rt: Any, plan: Plan, deployment: Any) -> tuple[str, str, dict]:
    outcome, reason = verify.evaluate(plan, deployment)
    return outcome, reason, verify.observed(deployment)


DEPLOYMENT_OPS = ResourceOps(
    kind=DEPLOYMENT,
    policy=DEPLOYMENT_POLICY,
    read=lambda rt, namespace, name: rt.apps().read_namespaced_deployment(name, namespace),
    patch=_patch_deployment,
    uid=lambda deployment: str(deployment.metadata.uid),
    resource_version=lambda deployment: deployment.metadata.resource_version,
    annotations=lambda deployment: deployment.metadata.annotations or {},
    check_unchanged=_deployment_unchanged,
    evaluate=_evaluate_deployment,
)


def read_hpa(autoscaling: Any, namespace: str, name: str) -> dict[str, Any]:
    """The HPA as the raw JSON the API server returned: the spec hash is taken over it
    (client models drop fields the client does not know)."""
    response = autoscaling.read_namespaced_horizontal_pod_autoscaler(
        name, namespace, _preload_content=False
    )
    value = json.loads(response.data)
    return value if isinstance(value, dict) else {}


def _patch_hpa(rt: Any, namespace: str, name: str, body: Any, dry_run: bool) -> dict[str, Any]:
    extra = {"dry_run": "All"} if dry_run else {}
    response = rt.autoscaling().patch_namespaced_horizontal_pod_autoscaler(
        name, namespace, body, _preload_content=False, **extra
    )
    value = json.loads(response.data)
    return value if isinstance(value, dict) else {}


def _hpa_unchanged(plan: Plan, hpa: Mapping[str, Any]) -> None:
    spec = hpa_spec(hpa)
    current = spec_hash(spec)
    bounds = hpa_bounds(spec)
    if current != plan.spec_from or bounds != (plan.min_from, plan.max_from):
        raise GuardError(
            CONFLICT,
            "HorizontalPodAutoscaler spec changed since the plan was made",
            {
                "spec": {"planned": plan.spec_from, "current": current},
                "bounds": {
                    "planned_from": {"min_replicas": plan.min_from, "max_replicas": plan.max_from},
                    "current": {"min_replicas": bounds[0], "max_replicas": bounds[1]},
                },
            },
        )


def read_target(rt: Any, plan: Plan) -> Any:
    """The Deployment an HPA plan's HPA scales, or None when it is gone."""
    try:
        return rt.apps().read_namespaced_deployment(plan.hpa_target, plan.namespace)
    except Exception as exc:
        # An ApiException; the kubernetes client is imported only by the tool Lambdas.
        if getattr(exc, "status", None) == 404:
            return None
        raise


def _evaluate_hpa(rt: Any, plan: Plan, hpa: Mapping[str, Any]) -> tuple[str, str, dict]:
    target = read_target(rt, plan)
    outcome, reason = evaluate_hpa(plan, hpa, target)
    return outcome, reason, observed_hpa(hpa, target)


HPA_OPS = ResourceOps(
    kind=HORIZONTAL_POD_AUTOSCALER,
    policy=HPA_POLICY,
    read=lambda rt, namespace, name: read_hpa(rt.autoscaling(), namespace, name),
    patch=_patch_hpa,
    uid=lambda hpa: str(hpa_metadata(hpa).get("uid") or ""),
    resource_version=lambda hpa: str(hpa_metadata(hpa).get("resourceVersion") or ""),
    annotations=hpa_annotations,
    check_unchanged=_hpa_unchanged,
    evaluate=_evaluate_hpa,
)

OPS = {DEPLOYMENT: DEPLOYMENT_OPS, HORIZONTAL_POD_AUTOSCALER: HPA_OPS}


def ops_for(plan: Plan) -> ResourceOps:
    """The operations for the plan's target_kind; an unknown kind is refused."""
    ops = OPS.get(kind_of(plan))
    if ops is None:
        raise GuardError(CONFLICT, f"unsupported target_kind {plan.target_kind!r}")
    return ops
