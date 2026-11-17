# Impact preview

Appendix to [the contract](../CONTRACT.md).

`data.impact` in the plan response, computed from objects read when the plan is made. Nothing in it blocks the plan; it is there for the approver. Values that depend on what happens after the change are named `projected_*`.

```json
{
  "pods": { "ready_now": 2, "from": 2, "to": 1, "added": 0, "removed": 1 },
  "pod_disruption_budgets": [
    { "name": "web", "min_available": 2, "max_unavailable": null,
      "current_healthy": 2, "desired_healthy": 2, "disruptions_allowed": 0 }
  ],
  "services": [ { "name": "web", "ready_endpoints_now": 2, "projected_ready_endpoints": 1 } ],
  "warnings": [
    { "code": "pdb_headroom", "message": "removing 1 pod(s) is more than PodDisruptionBudget web allows now (0); a scale-down is not an eviction, so the PodDisruptionBudget will not stop it" }
  ],
  "truncated": false
}
```

- `pods`: `ready_now` is the Deployment's `status.readyReplicas` (0 when unset); `from` and `to` are the planned replica counts; `added = max(0, to - from)`, `removed = max(0, from - to)`.
- `pod_disruption_budgets`: PodDisruptionBudgets in the namespace whose `spec.selector` matches the Deployment's pod template labels (`matchLabels` and `matchExpressions` with `In`, `NotIn`, `Exists`, `DoesNotExist`). As in `policy/v1`, an empty selector matches every pod and a missing selector matches none. `min_available` and `max_unavailable` are as given (an integer or a percentage string, `null` when unset); `current_healthy`, `desired_healthy` and `disruptions_allowed` are from `status` (`null` when unset). Sorted by name.
- `services`: Services in the namespace with a non-empty `spec.selector` that is a subset of the pod template labels; Services without a selector are skipped. `ready_endpoints_now` counts endpoints with `conditions.ready` true in the EndpointSlices labelled `kubernetes.io/service-name=<service>`; an unset `ready` counts as not ready, so the count can be low but never high, and a pod listed in several slices (dual-stack) is counted once (by `targetRef.uid`, otherwise by its addresses). `projected_ready_endpoints = max(0, ready_endpoints_now - ready_now) + to` is a projection: it assumes the new pods become ready and the Service's other backends stay as they are. While `status.readyReplicas` lags behind the EndpointSlices, the difference is counted as other backends, so the projection can be too high and miss `service_without_endpoints`; `scale_to_zero` still flags a scale to 0. Sorted by name.
- `warnings`, each `{code, message}`, in this order:
  - `revert_baseline_unhealthy`: the plan has `revert_on_failure` and the Deployment's `status.availableReplicas` (unset counts as 0) is below its current replica count. The verification compares with the target, not with the state before the change, so it may find the change degraded for a cause the change did not bring, and the revert would then undo it.
  - `scale_to_zero`: `to` is 0.
  - `pdb_headroom`: `removed` is greater than `disruptions_allowed` of a matching PodDisruptionBudget (unset counts as 0). A scale-down is not an eviction, so the PodDisruptionBudget will not stop it.
  - `service_without_endpoints`: a matching Service's `projected_ready_endpoints` is 0.

A restart's or rollback's preview describes the rollout instead of a change in the replica count:

```json
{
  "pods": { "ready_now": 2, "from": 2, "to": 2, "added": 0, "removed": 0, "replaced": 2 },
  "rollout": { "strategy": "RollingUpdate", "max_unavailable": 0, "max_surge": 1 },
  "pod_disruption_budgets": [ ... ],
  "services": [ { "name": "web", "ready_endpoints_now": 2, "projected_min_ready_endpoints": 2 } ],
  "warnings": [],
  "truncated": false
}
```

- `pods`: `from` and `to` are the current replica count, `added` and `removed` are 0, and `replaced` is the replica count: every pod is replaced.
- `rollout`: `strategy` is `Recreate` or `RollingUpdate` (also when the strategy is unset). For `RollingUpdate`, `max_unavailable` and `max_surge` are pod counts resolved as the Deployment controller resolves them: an integer is used as given, a percentage of the replica count rounds down for `maxUnavailable` and up for `maxSurge`, an unset value is 25 %, and if both resolve to 0, `max_unavailable` is 1. For `Recreate` both are `null`. A value that cannot be resolved fails the plan with `kubernetes_error`.
- `pod_disruption_budgets`: as above.
- `services`: as above, except that `projected_min_ready_endpoints = max(0, ready_endpoints_now - ready_now) + min(ready_now, replicas - down)` is the lowest point during the rollout, including its start, where `down` is `min(max_unavailable, replicas)`, or the replica count for `Recreate`. It assumes the new pods become ready; if they do not, the rollout stops at that point.
- `warnings`, in this order:
  - `recreate_strategy`: the strategy is `Recreate`; every pod is stopped before new ones start.
  - `pdb_headroom`: `down` is greater than `disruptions_allowed` of a matching PodDisruptionBudget (unset counts as 0). A rollout is not an eviction, so the PodDisruptionBudget will not stop it.
  - `service_without_endpoints`: a matching Service's `projected_min_ready_endpoints` is 0.
  - `rollout_in_progress`: `status.observedGeneration` is below `metadata.generation`, or `status.updatedReplicas` (unset counts as 0) is below the replica count. Information only.
  - `no_pods`: the replica count is 0; the template changes but no pod restarts.
  - `mutable_image_tag` (rollback only): an image that changes is not pinned by digest (`@sha256:`). The plan pins the image name, and the tag may now point to other content.
  - `config_not_rolled_back` (rollback only): the target template reads a ConfigMap or Secret (`envFrom`, `env[].valueFrom.configMapKeyRef` or `secretKeyRef`, or a `configMap`, `secret` or `projected` volume with such a source). A rollback does not restore their content.

A rollback's preview is the same as a restart's, with the two rollback-only warnings first, so the cut at 20 never drops them.

`strategy`, `generation` and the status fields are read from the same raw Deployment as `template_from`.

`truncated` is true when any of the three lists had more than 20 entries; each list then holds the first 20 (sorted), warnings having been computed over all of them, and the envelope's `truncated` is true as well. An object the preview cannot interpret, such as a selector with an unknown operator, fails the plan with `kubernetes_error`.

The lists are read as raw JSON (`_preload_content=False`), because the Kubernetes Python client refuses some valid objects, such as the placeholder EndpointSlice with `endpoints: null` of a Service without pods.
