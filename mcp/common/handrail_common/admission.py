"""Plan-time check of the cluster's admission policy (mcp/CONTRACT.md, Admission policy).

The read Lambda reads the ValidatingAdmissionPolicy and its binding, both named
kube-handrail-writer-bounds, on every plan, through the ClusterRole
kube-handrail-policy-reader (get on these two objects only). The plan is refused with
conflict unless the policy exists and selects the namespace, the binding refers to it,
selects the namespace and has validationActions Deny, and the policy's bounds for the
namespace equal the server's own (Settings.bounds_for).

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
# rendered by terraform/modules/k8s-manifests.
MIN_VARIABLE = "minReplicas"
MAX_VARIABLE = "maxReplicas"
# Set by the API server on every namespace, and immutable: the rendered manifests select
# namespaces by it.
NAME_LABEL = "kubernetes.io/metadata.name"
DENY = "Deny"

POLICY_MISSING = "policy_missing"
BINDING_MISSING = "binding_missing"
BINDING_OTHER_POLICY = "binding_other_policy"
BINDING_NOT_DENY = "binding_not_deny"
POLICY_NOT_SELECTING = "policy_not_selecting_namespace"
BINDING_NOT_SELECTING = "binding_not_selecting_namespace"
BOUNDS_MISMATCH = "bounds_mismatch"
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
