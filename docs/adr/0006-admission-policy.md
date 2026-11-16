# ADR 0006: Admission policy as defense in depth

- Status: Accepted
- Date: 2026-11-06

Implemented in [Milestone 2](../roadmap.md#milestone-2-verified-changes-more-actions-accountable-agents) and tested on a test cluster; the policy's cases run in CI on kind. See [Admission policy](../../mcp/CONTRACT.md#admission-policy) for the exact rules.

## Context

Every limit on a change is enforced by the guard in the write Lambda. RBAC narrows what the writer identity can touch (Deployments in one namespace, `get` and `patch` only) but not by how much: a writer that skipped the guard, through a bug in the guard, a changed environment variable or stolen credentials of the write role, could set any replica count in the sandbox namespace.

The original plan was one policy file compiled to Cedar at the gateway, to a ValidatingAdmissionPolicy and to the guard, with a CI check that all three agree. That needs a policy engine at the gateway and a compiler, and Cedar at the gateway sees neither approval-triggered applies nor reverts, which never pass the gateway.

## Decision

- **One ValidatingAdmissionPolicy with one binding, `kube-handrail-writer-bounds`**, checks the replica bounds in the cluster: a writer `UPDATE` of a Deployment or its `scale` subresource that changes the replica count must leave it within `[min, max]`.
- **Scope is fixed by selection, not by CEL.** The policy and the binding both select the sandbox namespace by `kubernetes.io/metadata.name`, and a `matchCondition` selects the group `kube-handrail-writer`. On a cluster shared with other workloads, a wrong expression can only affect that namespace: a failing validation refuses only the writer, and a `matchCondition` that cannot be evaluated falls to `failurePolicy`, which in `Deny` would refuse Deployment updates by every user in the sandbox namespace. With that scope, `failurePolicy: Fail` is acceptable; the committed `matchCondition` cannot error.
- **Only a change of the count is checked.** Restart and rollback keep the count and change the template; a Deployment a human scaled outside the bounds can still be restarted. The template is not checked: the policy cannot compare it with a ReplicaSet's, and the guard's hash check already pins it.
- **The bounds are static in the file**, set with `sed` like the namespace in the RBAC. They must equal `min_replicas` and `max_replicas`. A parameter resource would keep them in one object but adds a CRD or ConfigMap and a binding parameter for two numbers.
- **Audit before enforcement.** The committed binding is `[Warn, Audit]`; an operator switches it to `[Deny]` after checking the warnings.
- **The guard maps the policy's denial to `admission_denied`**, recognized by status 422 and the API server's wording that names this policy and binding, never by status alone. The plan is used (the refusal comes from the dry run after the consume) and the agent does not retry.
- **Cedar is deferred.** It would add a second engine for the same bounds at a layer that does not see every write.

## Consequences

- A guard that lets an out-of-bounds count through is caught in the cluster once the binding is in `Deny`, for every apply channel and for reverts.
- The bounds live in two places, Terraform and the file. A narrower policy refuses applies the guard allowed, visibly (`admission_denied`); a wider one only weakens the second check.
- Nothing else is checked in the cluster: approvals, history, budgets and the template remain the guard's alone.
- `kubectl apply` of the file resets the binding to `[Warn, Audit]`; the operator patches it again.
- Needs Kubernetes 1.30 or later. Older clusters run without this layer.

## Alternatives considered

### A. Single policy file compiled to Cedar, admission policy and guard

Planned originally. Deferred: much more machinery than the one rule the cluster can check on its own, and Cedar misses the approval channel and reverts.

### B. Restrict the pod template or the annotations

Restart and rollback must change the template, and the annotations carry the plan ID. A rule that knew which changes are legitimate would need the plan, which the cluster does not have.

### C. Deny create and delete for the writer in the policy

RBAC already gives the writer neither. Left out to keep the policy to the one rule RBAC cannot express.
