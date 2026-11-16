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
"""

import json
from collections.abc import Callable, Mapping
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


def _refuse(reason: str, message: str, **details: Any) -> GuardError:
    return GuardError(
        CONFLICT,
        f"the cluster's admission policy {POLICY_NAME} does not back this plan: {message}; "
        + _HINT,
        {"check": CHECK, "reason": reason, "policy": POLICY_NAME, **details},
    )


def _read(call: Callable[..., Any], name: str, what: str) -> dict[str, Any] | None:
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
        raise _refuse(UNREADABLE, f"cannot read the {what} ({cause})", status=status) from None
    try:
        value = json.loads(response.data)
    except (ValueError, AttributeError, TypeError):
        raise _refuse(UNREADABLE, f"the {what} is not JSON") from None
    if not isinstance(value, dict):
        raise _refuse(UNREADABLE, f"the {what} is not an object")
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


def _covers(rule: Any, resource: str) -> bool:
    """Whether one resourceRules entry matches UPDATE of apps/v1 `resource` in a
    namespace, for every object name."""
    if not isinstance(rule, Mapping):
        return False

    def has(key: str, *values: str) -> bool:
        listed = rule.get(key) or []
        return isinstance(listed, list) and any(v in listed for v in (*values, "*"))

    parent, _, sub = resource.partition("/")
    names = [resource, f"{parent}/*"] + ([f"*/{sub}"] if sub else [])
    return (
        has("apiGroups", "apps")
        and has("apiVersions", "v1")
        and has("operations", "UPDATE")
        and has("resources", *names)
        and not rule.get("resourceNames")
        and rule.get("scope") in (None, "*", "Namespaced")
    )


def _covers_all(rules: Any) -> bool:
    rules = rules if isinstance(rules, list) else []
    return all(any(_covers(rule, resource) for rule in rules) for resource in RESOURCES)


def _empty(value: Any) -> bool:
    return value is None or value == {} or value == []


def _policy_modified(spec: Mapping[str, Any]) -> str | None:
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
    if not _covers_all(constraints.get("resourceRules")):
        return "resourceRules do not cover UPDATE of deployments and deployments/scale"
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
    for name, expression in FIXED_VARIABLES.items():
        if variables.get(name) != expression:
            return f"variable {name} differs from the rendered one"
    validations = spec.get("validations") or []
    if not isinstance(validations, list) or not any(
        isinstance(v, Mapping) and _normal(v.get("expression")) == VALIDATION for v in validations
    ):
        return "no validation has the rendered expression"
    return None


def _binding_modified(spec: Mapping[str, Any]) -> str | None:
    resources = spec.get("matchResources") or {}
    if not isinstance(resources, Mapping):
        return "matchResources is not an object"
    if not _empty(resources.get("objectSelector")):
        return "matchResources has an objectSelector"
    if not _empty(resources.get("excludeResourceRules")):
        return "matchResources has excludeResourceRules"
    rules = resources.get("resourceRules")
    if not _empty(rules) and not _covers_all(rules):
        return "resourceRules do not cover UPDATE of deployments and deployments/scale"
    return None


def check_admission_policy(api: Any, namespace: str, bounds: tuple[int, int]) -> None:
    """Raise conflict unless the cluster's policy and binding back the bounds in the
    namespace. `api` is an AdmissionregistrationV1Api."""
    policy = _read(api.read_validating_admission_policy, POLICY_NAME, "policy")
    if policy is None:
        raise _refuse(POLICY_MISSING, "the policy does not exist")
    binding = _read(api.read_validating_admission_policy_binding, BINDING_NAME, "binding")
    if binding is None:
        raise _refuse(BINDING_MISSING, "the binding does not exist")

    policy_spec = policy.get("spec") or {}
    binding_spec = binding.get("spec") or {}
    if binding_spec.get("policyName") != POLICY_NAME:
        raise _refuse(BINDING_OTHER_POLICY, "the binding refers to another policy")
    actions = binding_spec.get("validationActions") or []
    if not isinstance(actions, list) or DENY not in actions:
        raise _refuse(
            BINDING_NOT_DENY,
            "the binding does not deny (validationActions has no Deny)",
            validation_actions=actions if isinstance(actions, list) else None,
        )
    if not selects((policy_spec.get("matchConstraints") or {}).get("namespaceSelector"), namespace):
        raise _refuse(POLICY_NOT_SELECTING, f"the policy does not select namespace {namespace}")
    if not selects((binding_spec.get("matchResources") or {}).get("namespaceSelector"), namespace):
        raise _refuse(BINDING_NOT_SELECTING, f"the binding does not select namespace {namespace}")

    modified = _policy_modified(policy_spec)
    if modified:
        raise _refuse(POLICY_MODIFIED, f"the policy was changed: {modified}")
    modified = _binding_modified(binding_spec)
    if modified:
        raise _refuse(BINDING_MODIFIED, f"the binding was changed: {modified}")

    variables = policy_spec.get("variables")
    found = (
        _bound(variables, MIN_VARIABLE, namespace),
        _bound(variables, MAX_VARIABLE, namespace),
    )
    if found != tuple(bounds):
        minimum, maximum = bounds
        raise _refuse(
            BOUNDS_MISMATCH,
            f"its bounds for namespace {namespace} are {list(found)}, the server's "
            f"[{minimum}, {maximum}]",
            expected={"min_replicas": minimum, "max_replicas": maximum},
            found={"min_replicas": found[0], "max_replicas": found[1]},
        )
