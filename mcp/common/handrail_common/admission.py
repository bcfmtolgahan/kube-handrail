"""Plan-time check of the cluster's admission policy (mcp/CONTRACT.md, Admission policy).

The read Lambda reads the ValidatingAdmissionPolicy and its binding, both named
kube-handrail-writer-bounds, on every plan, through the ClusterRole
kube-handrail-policy-reader (get on these two objects only). The plan is refused with
conflict unless the policy exists and selects the namespace, the binding refers to it,
selects the namespace and has validationActions Deny, and the policy's bounds for the
namespace equal the server's own (Settings.bounds_for).

It also refuses a policy whose enforcing parts differ from what Terraform renders: a
failurePolicy other than Fail, a matchCondition or validation expression other than the
rendered one, resourceRules that no longer cover UPDATE of deployments and
deployments/scale, an objectSelector, excludeResourceRules, or a paramKind. That catches
the edits that would switch the policy off while leaving its bounds in place. It is a
check for drift and for those edits, not proof against every change by someone who may
edit the policy: anyone with that right is trusted as much as the policy itself.

It fails closed: a policy or binding that cannot be read (no RBAC, an API error, a network
error) refuses the plan too, so a cluster without the backstop is never planned against
silently. Only ADMISSION_CHECK=false (Terraform admission_check = false) turns it off.

plan_adjust_hpa checks the second policy and binding, kube-handrail-writer-hpa-bounds
(HPA_POLICY), the same way: the same bounds variables, its own validations and
resourceRules covering UPDATE of autoscaling/v2 horizontalpodautoscalers.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from handrail_common.envelope import CONFLICT
from handrail_common.validation import GuardError

POLICY_NAME = "kube-handrail-writer-bounds"
BINDING_NAME = POLICY_NAME
# The preflight entry and details.check of a refusal.
CHECK = "admission_policy_match"
# The policy's CEL variables: JSON objects (valid CEL map literals) of namespace => count,
# rendered by terraform/modules/namespace-policy.
MIN_VARIABLE = "minReplicas"
MAX_VARIABLE = "maxReplicas"
# Set by the API server on every namespace, and immutable: the rendered manifests select
# namespaces by it.
NAME_LABEL = "kubernetes.io/metadata.name"
DENY = "Deny"

# The enforcing parts of terraform/modules/namespace-policy/templates/
# admission-policy.yaml.tftpl, compared with runs of whitespace collapsed (the template
# folds them over lines). tests/unit/test_namespace_policy.py checks them against the
# rendered goldens.
MATCH_CONDITION = (
    "has(request.userInfo.groups) && 'kube-handrail-writer' in request.userInfo.groups"
)
VALIDATION = (
    "variables.replicas == variables.oldReplicas || (variables.bounded && "
    "variables.replicas >= variables.minReplicas[request.namespace] && "
    "variables.replicas <= variables.maxReplicas[request.namespace])"
)
FIXED_VARIABLES = {
    "bounded": (
        "request.namespace in variables.minReplicas && request.namespace in variables.maxReplicas"
    ),
    "replicas": "has(object.spec.replicas) ? object.spec.replicas : 0",
    "oldReplicas": "has(oldObject.spec.replicas) ? oldObject.spec.replicas : 0",
}
RESOURCES = ("deployments", "deployments/scale")


@dataclass(frozen=True)
class PolicyShape:
    """One rendered policy and binding: their name, the validation expressions that must all
    be present, the variables besides the bounds, and the (group, version, resource) rules
    the resourceRules must cover for UPDATE."""

    name: str
    validations: tuple[str, ...]
    fixed_variables: Mapping[str, str]
    resources: tuple[tuple[str, str, str], ...]
    # For messages: what the resourceRules must cover.
    covers: str


DEPLOYMENT_POLICY = PolicyShape(
    name=POLICY_NAME,
    validations=(VALIDATION,),
    fixed_variables=FIXED_VARIABLES,
    resources=tuple(("apps", "v1", resource) for resource in RESOURCES),
    covers="UPDATE of deployments and deployments/scale",
)
# The HPA policy (terraform/modules/namespace-policy/templates/admission-policy.yaml.tftpl).
HPA_POLICY_NAME = "kube-handrail-writer-hpa-bounds"
HPA_BOUNDS_VALIDATION = (
    "(variables.hpaMin == variables.oldHpaMin && "
    "object.spec.maxReplicas == oldObject.spec.maxReplicas) || (variables.bounded && "
    "variables.hpaMin >= variables.minReplicas[request.namespace] && "
    "object.spec.maxReplicas <= variables.maxReplicas[request.namespace])"
)
HPA_FROZEN_VALIDATION = (
    "object.spec.scaleTargetRef == oldObject.spec.scaleTargetRef && "
    "has(object.spec.metrics) == has(oldObject.spec.metrics) && "
    "(!has(object.spec.metrics) || object.spec.metrics == oldObject.spec.metrics) && "
    "has(object.spec.behavior) == has(oldObject.spec.behavior) && "
    "(!has(object.spec.behavior) || object.spec.behavior == oldObject.spec.behavior)"
)
HPA_POLICY = PolicyShape(
    name=HPA_POLICY_NAME,
    validations=(HPA_BOUNDS_VALIDATION, HPA_FROZEN_VALIDATION),
    fixed_variables={
        "bounded": FIXED_VARIABLES["bounded"],
        "hpaMin": "has(object.spec.minReplicas) ? object.spec.minReplicas : 1",
        "oldHpaMin": "has(oldObject.spec.minReplicas) ? oldObject.spec.minReplicas : 1",
    },
    resources=(("autoscaling", "v2", "horizontalpodautoscalers"),),
    covers="UPDATE of autoscaling/v2 horizontalpodautoscalers",
)

POLICY_MISSING = "policy_missing"
BINDING_MISSING = "binding_missing"
BINDING_OTHER_POLICY = "binding_other_policy"
BINDING_NOT_DENY = "binding_not_deny"
POLICY_NOT_SELECTING = "policy_not_selecting_namespace"
BINDING_NOT_SELECTING = "binding_not_selecting_namespace"
BOUNDS_MISMATCH = "bounds_mismatch"
POLICY_MODIFIED = "policy_modified"
BINDING_MODIFIED = "binding_modified"
UNREADABLE = "unreadable"

_HINT = "an operator must apply the rendered manifests (make upgrade UPGRADE_FLAGS=--with-k8s)"


def _refuse(reason: str, message: str, policy: str = POLICY_NAME, **details: Any) -> GuardError:
    return GuardError(
        CONFLICT,
        f"the cluster's admission policy {policy} does not back this plan: {message}; " + _HINT,
        {"check": CHECK, "reason": reason, "policy": policy, **details},
    )


def _read(
    call: Callable[..., Any], name: str, what: str, policy: str = POLICY_NAME
) -> dict[str, Any] | None:
    """The object as raw JSON, or None when it does not exist. Anything else that keeps it
    from being read refuses the plan (fail closed)."""
    try:
        response = call(name=name, _preload_content=False)
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status == 404:
            return None
        # Status only: the response body can echo cluster internals.
        cause = f"HTTP {status}" if isinstance(status, int) else type(exc).__name__
        raise _refuse(
            UNREADABLE, f"cannot read the {what} ({cause})", policy, status=status
        ) from None
    try:
        value = json.loads(response.data)
    except (ValueError, AttributeError, TypeError):
        raise _refuse(UNREADABLE, f"the {what} is not JSON", policy) from None
    if not isinstance(value, dict):
        raise _refuse(UNREADABLE, f"the {what} is not an object", policy)
    return value


def _requirement_holds(requirement: Any, namespace: str) -> bool:
    """One matchExpressions entry, for a namespace known only by its name label. A
    requirement on any other label cannot be shown to hold, so it counts as not holding."""
    if not isinstance(requirement, Mapping) or requirement.get("key") != NAME_LABEL:
        return False
    operator = requirement.get("operator")
    values = requirement.get("values") or []
    if not isinstance(values, list):
        return False
    if operator == "In":
        return namespace in values
    if operator == "NotIn":
        return namespace not in values
    return operator == "Exists"


def selects(selector: Any, namespace: str) -> bool:
    """Whether a namespaceSelector is sure to select the namespace. Unset or empty selects
    every namespace; a label other than the name label makes it unsure, which is not."""
    if selector is None:
        return True
    if not isinstance(selector, Mapping):
        return False
    labels = selector.get("matchLabels") or {}
    expressions = selector.get("matchExpressions") or []
    if not isinstance(labels, Mapping) or not isinstance(expressions, list):
        return False
    if any(key != NAME_LABEL or value != namespace for key, value in labels.items()):
        return False
    return all(_requirement_holds(r, namespace) for r in expressions)


def _bound(variables: Any, name: str, namespace: str) -> int | None:
    """The count the policy's CEL variable `name` holds for the namespace, or None."""
    for variable in variables if isinstance(variables, list) else []:
        if isinstance(variable, Mapping) and variable.get("name") == name:
            try:
                value = json.loads(str(variable.get("expression")))
            except ValueError:
                return None
            count = value.get(namespace) if isinstance(value, dict) else None
            return count if isinstance(count, int) and not isinstance(count, bool) else None
    return None


def _normal(expression: Any) -> str:
    return " ".join(str(expression).split())


def _covers(rule: Any, resource: str, group: str = "apps", version: str = "v1") -> bool:
    """Whether one resourceRules entry matches UPDATE of `group`/`version` `resource` in a
    namespace, for every object name."""
    if not isinstance(rule, Mapping):
        return False

    def has(key: str, *values: str) -> bool:
        listed = rule.get(key) or []
        return isinstance(listed, list) and any(v in listed for v in (*values, "*"))

    parent, _, sub = resource.partition("/")
    names = [resource, f"{parent}/*"] + ([f"*/{sub}"] if sub else [])
    return (
        has("apiGroups", group)
        and has("apiVersions", version)
        and has("operations", "UPDATE")
        and has("resources", *names)
        and not rule.get("resourceNames")
        and rule.get("scope") in (None, "*", "Namespaced")
    )


def _covers_all(rules: Any, shape: PolicyShape = DEPLOYMENT_POLICY) -> bool:
    rules = rules if isinstance(rules, list) else []
    return all(
        any(_covers(rule, resource, group, version) for rule in rules)
        for group, version, resource in shape.resources
    )


def _empty(value: Any) -> bool:
    return value is None or value == {} or value == []


def _policy_modified(spec: Mapping[str, Any], shape: PolicyShape = DEPLOYMENT_POLICY) -> str | None:
    """What makes the policy's enforcing parts differ from the rendered ones, or None."""
    if spec.get("failurePolicy", "Fail") != "Fail":
        return "failurePolicy is not Fail"
    if not _empty(spec.get("paramKind")):
        return "it has a paramKind"
    constraints = spec.get("matchConstraints") or {}
    if not isinstance(constraints, Mapping):
        return "matchConstraints is not an object"
    if not _empty(constraints.get("objectSelector")):
        return "matchConstraints has an objectSelector"
    if not _empty(constraints.get("excludeResourceRules")):
        return "matchConstraints has excludeResourceRules"
    if not _covers_all(constraints.get("resourceRules"), shape):
        return f"resourceRules do not cover {shape.covers}"
    conditions = spec.get("matchConditions") or []
    if not isinstance(conditions, list) or [
        _normal(c.get("expression")) if isinstance(c, Mapping) else None for c in conditions
    ] != [MATCH_CONDITION]:
        return "matchConditions differ from the rendered one"
    variables = {
        v.get("name"): _normal(v.get("expression"))
        for v in spec.get("variables") or []
        if isinstance(v, Mapping)
    }
    for name, expression in shape.fixed_variables.items():
        if variables.get(name) != expression:
            return f"variable {name} differs from the rendered one"
    validations = spec.get("validations") or []
    present = {
        _normal(v.get("expression"))
        for v in (validations if isinstance(validations, list) else [])
        if isinstance(v, Mapping)
    }
    if not all(expression in present for expression in shape.validations):
        return "a rendered validation expression is missing"
    return None


def _binding_modified(
    spec: Mapping[str, Any], shape: PolicyShape = DEPLOYMENT_POLICY
) -> str | None:
    resources = spec.get("matchResources") or {}
    if not isinstance(resources, Mapping):
        return "matchResources is not an object"
    if not _empty(resources.get("objectSelector")):
        return "matchResources has an objectSelector"
    if not _empty(resources.get("excludeResourceRules")):
        return "matchResources has excludeResourceRules"
    rules = resources.get("resourceRules")
    if not _empty(rules) and not _covers_all(rules, shape):
        return f"resourceRules do not cover {shape.covers}"
    return None


def check_admission_policy(
    api: Any, namespace: str, bounds: tuple[int, int], shape: PolicyShape = DEPLOYMENT_POLICY
) -> None:
    """Raise conflict unless the cluster's policy and binding back the bounds in the
    namespace. `api` is an AdmissionregistrationV1Api; `shape` names the policy (the
    Deployment one, or HPA_POLICY for plan_adjust_hpa)."""
    name = shape.name

    def refuse(reason: str, message: str, **details: Any) -> GuardError:
        return _refuse(reason, message, name, **details)

    policy = _read(api.read_validating_admission_policy, name, "policy", name)
    if policy is None:
        raise refuse(POLICY_MISSING, "the policy does not exist")
    binding = _read(api.read_validating_admission_policy_binding, name, "binding", name)
    if binding is None:
        raise refuse(BINDING_MISSING, "the binding does not exist")

    policy_spec = policy.get("spec") or {}
    binding_spec = binding.get("spec") or {}
    if binding_spec.get("policyName") != name:
        raise refuse(BINDING_OTHER_POLICY, "the binding refers to another policy")
    actions = binding_spec.get("validationActions") or []
    if not isinstance(actions, list) or DENY not in actions:
        raise refuse(
            BINDING_NOT_DENY,
            "the binding does not deny (validationActions has no Deny)",
            validation_actions=actions if isinstance(actions, list) else None,
        )
    if not selects((policy_spec.get("matchConstraints") or {}).get("namespaceSelector"), namespace):
        raise refuse(POLICY_NOT_SELECTING, f"the policy does not select namespace {namespace}")
    if not selects((binding_spec.get("matchResources") or {}).get("namespaceSelector"), namespace):
        raise refuse(BINDING_NOT_SELECTING, f"the binding does not select namespace {namespace}")

    modified = _policy_modified(policy_spec, shape)
    if modified:
        raise refuse(POLICY_MODIFIED, f"the policy was changed: {modified}")
    modified = _binding_modified(binding_spec, shape)
    if modified:
        raise refuse(BINDING_MODIFIED, f"the binding was changed: {modified}")

    variables = policy_spec.get("variables")
    found = (
        _bound(variables, MIN_VARIABLE, namespace),
        _bound(variables, MAX_VARIABLE, namespace),
    )
    if found != tuple(bounds):
        minimum, maximum = bounds
        raise refuse(
            BOUNDS_MISMATCH,
            f"its bounds for namespace {namespace} are {list(found)}, the server's "
            f"[{minimum}, {maximum}]",
            expected={"min_replicas": minimum, "max_replicas": maximum},
            found={"min_replicas": found[0], "max_replicas": found[1]},
        )
