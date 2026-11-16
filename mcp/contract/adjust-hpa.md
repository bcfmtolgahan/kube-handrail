# Adjust HPA

Appendix to [the contract](../CONTRACT.md#adjust-hpa). Status: implemented, not yet tested on a cluster; the findings below come from kind (Kubernetes 1.31.4, metrics-server v0.7.2).

`plan_adjust_hpa(namespace, name, min_replicas?, max_replicas?, override_manual_change?, verify_window_seconds?, revert_on_failure?)` and `apply_adjust_hpa(plan_id, ...the same)` change a HorizontalPodAutoscaler's `minReplicas` and `maxReplicas`. `name` is the HPA's. The action is `adjust_hpa`; a namespace allows it only when its `actions` list it (it is never a default). Below are the differences from a scale ([Guard checks](../CONTRACT.md#guard-checks)).

## Arguments and record

- At least one of `min_replicas` and `max_replicas`, each an integer; an omitted bound stays as it is. `args` and `args_hash` carry each bound only when given, `override_manual_change: true`, `verify_window_seconds` and `revert_on_failure: true` as for a scale.
- The record has `target_kind: "HorizontalPodAutoscaler"`, `hpa_target` (the Deployment in `scaleTargetRef`), `min_from`, `min_to`, `max_from`, `max_to`, `spec_from` and `spec_to` (`sha256:` over the HPA's raw `spec`, hashed like a pod template, before and with the new bounds), `generation: -1`, the HPA's `uid` and `resourceVersion`, and no `template_from`/`template_to`. `replicas_from` is the target Deployment's `spec.replicas` (unset counts as 1); `replicas_to` is that count clamped to the new bounds: the pods the change moves.
- With `revert_on_failure`, the consume copies `min_from` and `max_from` to `revert_min_to` and `revert_max_to` (not `revert_to`), in the same conditional update; the read role cannot write them (`verify_attributes` in `terraform/verify.tf`).

## Plan

1. Checks 1-2 with the action `adjust_hpa`; the bounds are checked in step 5, because an omitted one is the live one.
2. Freezes, the agent checks, then the plan-time admission check against `kube-handrail-writer-hpa-bounds` (below) instead of the Deployment policy.
3. Read the HPA (`not_found` when missing). Its `scaleTargetRef` must be a Deployment of an `apps/*` version (`invalid_argument` otherwise). Read that Deployment. Either one GitOps-managed is `gitops_managed`.
4. A Deployment scaled to 0 is `conflict`, `details: {"reason": "scaling_disabled"}`: the HPA controller does not act on it.
5. The new bounds: `min_to >= 1` and `min_to <= max_to` (`invalid_argument`); both within the namespace's `min_replicas` and `max_replicas` (`out_of_bounds`, `details` naming the bound), because the admission policy checks both on any change; something must change, and the two bounds must not move in opposite directions (`invalid_argument`: change them in two plans, so the change has one direction).
6. Blast radius: `|clamp(current, min_to, max_to) - current| <= max_step(current)`, with `current` the Deployment's `spec.replicas`.
7. With `revert_on_failure`, the current bounds must lie within the namespace's (`out_of_bounds`), since the revert sets them.
8. History, by the HPA's `object_key` (`<namespace>/<hpa>/<hpa uid>`, its own history): the bounds manual-change rule (the HPA's last-plan annotation names a plan whose `min_to`/`max_to` differ from the live bounds: `manual_change` with `details` `{last_applied_to, current}`, each `{min_replicas, max_replicas}`, unless `override_manual_change`), cooldown, flapping and object budget. Flapping takes the direction of the bounds (`plans.bounds_direction`: +1 when a bound rises, -1 when one falls), not of the pods, which the HPA moves on its own.
9. Budgets and the impact preview as for a scale from `replicas_from` to `replicas_to` on the target Deployment. The warning `hpa_metrics_unavailable` is added when the HPA's `ScalingActive` condition is `False` (other than `ScalingDisabled`); the condition's text is never returned. `revert_baseline_unhealthy` as for a scale.

`data.diff` is `{"hpa": {"min_replicas": {"from", "to"}, "max_replicas": {"from", "to"}, "scale_target": "<deployment>"}, "replicas": {"from", "to"}}`. `preflight`: `arguments`, `agent_enabled`, `namespace_allowed`, `action_allowed`, `admission_policy_match`, `not_gitops_managed`, `scale_target_supported`, `hpa_bounds`, `blast_radius`, `revert_target_in_bounds` (with revert), `no_manual_change`, `cooldown`, `not_flapping`, `object_budget`, `global_budget`, `agent_budget` (no `plan_rate`). `verification.revert_to` is `{"min_replicas", "max_replicas"}`.

## Apply

- Step 2: the arguments as on plan. After the consume the record must have `target_kind`, `min_from`, `max_from`, `spec_from`, `spec_to` and `hpa_target`, `min_to` equal to `min_replicas` (or `min_from` when omitted), `max_to` likewise, and the override of the arguments (`plan_mismatch` otherwise). Step 3: the new bounds against the namespace's bounds at apply time (`out_of_bounds`), instead of the blast-radius step.
- Step 4, on every attempt: the HPA read as raw JSON; the same `uid`, not GitOps-managed, and the hash of its spec equal to `spec_from` with bounds `min_from`/`max_from` (`conflict`, `details` `{spec: {planned, current}, bounds: {planned_from, current}}`). The HPA has no `generation`, so the spec hash replaces the generation check; `resourceVersion` is only the patch precondition, and a 409 caused by the HPA controller's status writes is retried after the same checks.
- Step 7, before the history checks: the target Deployment is read again (still the HPA's `scaleTargetRef`, else `conflict`; not GitOps-managed; not scaled to 0, else `conflict` with `details.reason` `scaling_disabled`) and the blast radius is computed again on its current `spec.replicas`; then the bounds manual-change rule, cooldown, flapping and object budget, then the budgets.
- The patch is a merge patch with only `metadata.resourceVersion`, the annotations of step 6 (on the HPA) and `spec.minReplicas` and `spec.maxReplicas`, through `autoscaling/v2`. A denial by `kube-handrail-writer-hpa-bounds` is `admission_denied` with that policy and binding in `details`.

A successful apply's `data` is `plan_id`, `namespace`, `name`, `min_replicas` and `max_replicas` (each `{from, to}`), `scale_target`, `resource_version`, `approved` and `correlation_id`.

## Verification and revert

The verifier reads the HPA and its target Deployment (the writer may `get` Deployments wherever it may patch HPAs). First match wins:

| Outcome | Reason | When |
|---|---|---|
| `unknown` | `replaced` | the HPA's `uid` differs |
| `unknown` | `superseded` | its last-plan annotation names another plan |
| `unknown` | `spec_changed` | its spec hash is not `spec_to`, or its bounds are not `min_to`/`max_to` |
| `unknown` | `not_found`, `paused`, `not_observed` | the target Deployment is gone, paused, or its controller has not seen its latest spec |
| `degraded` | `not_able_to_scale` | the HPA's `AbleToScale` is `False` |
| `degraded` | `progress_deadline_exceeded`, `replica_failure` | as for a scale, on the target Deployment |
| `degraded` | `target_not_reached` | the HPA's `status.currentReplicas` or the target's `spec.replicas` is outside `[min_to, max_to]`, or the target has fewer available pods than `min(spec.replicas, min_to)` (above `min_to` the HPA may be scaling up for load) |
| `healthy` | `target_reached` | otherwise |

`observed` is `{min_replicas, max_replicas, current_replicas, desired_replicas, conditions, target}`, where `conditions` are the HPA's (type, status, reason) and `target` is the Deployment's observation as for a scale. Metrics that are unavailable do not make the outcome degraded on their own.

A degraded change with `revert_on_failure` is reverted like a scale ([Revert](../CONTRACT.md#revert)), with these differences: the pinned targets are `revert_min_to` and `revert_max_to` (a record without both is `refused`, `plan_mismatch`); both must lie within the namespace's bounds and the namespace must still allow `adjust_hpa`; the revert record is an `adjust_hpa` plan with `target_kind`, `min_from`/`max_from` and `spec_from` equal to the parent's `min_to`/`max_to` and `spec_to`, and `min_to`/`max_to` the pinned bounds; the patch writes both bounds, only on the spec the parent's apply left. The revert restores the bounds, not the pods: the HPA controller decides the count within them.

`get_plan_outcome` adds `target_kind` and `hpa` (the bounds and `scale_target`, as in the diff) for an HPA adjust; `replicas` are the target's pods. The approval view's `change` is the diff; the notification names the HPA and its Deployment, both bounds and the pod change. The audit summary copies `target_kind` and `hpa_target`; `scripts/audit.py verify` accepts `revert_min_to` and `revert_max_to` in the consume and treats the HPA fields set at plan time as pinned.

## RBAC and admission

- The writer Role gets `get` and `patch` on `autoscaling/horizontalpodautoscalers` only in namespaces whose `actions` include `adjust_hpa`, and `patch` on Deployments only where `scale`, `restart` or `rollback` is allowed (an `adjust_hpa`-only namespace gets `get` on Deployments, for the verification). The reader already lists and gets HPAs.
- `kube-handrail-writer-hpa-bounds` (rendered with the Deployment policy into `admission-policy.yaml`, same namespaces, same `minReplicas`/`maxReplicas` maps, `failurePolicy: Fail`, the same writer `matchCondition`, its binding in `admission_mode`) matches `UPDATE` of `autoscaling/v2` `horizontalpodautoscalers`; requests through `autoscaling/v1` reach it converted (matchPolicy `Equivalent`). Its validations: a change of `minReplicas` (default 1) or `maxReplicas` must keep both within the namespace's bounds, and `scaleTargetRef`, `metrics` and `behavior` must equal the old object's. It covers every namespace of the policy, whatever RBAC grants.
- `tests/admission/run.sh` creates an HPA per namespace (its target does not exist, so the controller changes nothing) and runs dry runs as the writer: a higher `minReplicas` within the bounds is admitted; `maxReplicas` over the maximum (also through `autoscaling/v1`), `minReplicas` below a minimum above 1, and a changed `scaleTargetRef`, `metrics` or `behavior` are refused by this policy; another user is admitted. Its RBAC cases follow `ACTIONS` and `ACTIONS_2`.
- `plan_adjust_hpa` checks it as the [plan-time check](../CONTRACT.md#plan-time-check) checks the Deployment policy, with its own expressions and resource rules (`admission.HPA_POLICY`); `details.policy` names it. The policy reader may `get` both policies and both bindings.

## Findings on kind

Recorded while building Milestone 8 (Kubernetes 1.31.4, the CI node image), before any EKS run.

1. **`metadata.generation`: not set on HorizontalPodAutoscalers**, through `autoscaling/v1` or `v2`, before or after a spec change, and the status has no `observedGeneration`. `resourceVersion` changes on every status write of the HPA controller (observed without any spec change) and on annotation changes, so comparing it would refuse nearly every apply. Hence the spec hash (`spec_from`, `spec_to`) for the spec checks, and `resourceVersion` only as the patch precondition with the 409 retry.
2. **The controller applies `minReplicas` and `maxReplicas` with its metrics broken.** With no metrics API (and with BestEffort pods, which have no CPU request), raising `minReplicas` from 1 to 2 scaled the Deployment to 2 (`SuccessfulRescale`, "Current number of replicas below Spec.MinReplicas") while `ScalingActive` stayed `False` (`FailedGetResourceMetric`); lowering `maxReplicas` below the count scaled it down the same way. Hence `hpa_metrics_unavailable` is a warning, not a refusal. `desiredReplicas` was 0 until the first rescale, so the verification does not rely on it.
3. **`status.currentReplicas` is the target's `spec.replicas`, not its running pods**, read at the start of a sync, so it trails a patch by one sync (about 15 seconds). With a ResourceQuota admitting one pod, min 1 to 2 left `currentReplicas` 2 while only one pod ran (`ReplicaFailure` `FailedCreate`). Hence the verification also checks the target's available pods.
4. **A revert restores the bounds, not the pods.** In that quota case, setting `minReplicas` back to 1 left the Deployment at 2 replicas: with metrics unavailable the controller does not scale down within the range, and with metrics it would only after its scale-down stabilization (5 minutes by default). The revert record is verified against the restored bounds; in that case it was `degraded` (`replica_failure`) too, since the pod the quota refused was still wanted.
