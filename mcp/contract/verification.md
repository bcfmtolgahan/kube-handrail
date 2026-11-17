# Verification and revert

Appendix to [the contract](../CONTRACT.md).

Every applied plan is checked once, `verify_window_seconds` after its apply, and `get_plan_outcome` reports whether the change reached its target.

## Arguments

- `verify_window_seconds` (every `plan_*` and `apply_*` tool): an integer from `VERIFY_WINDOW_MIN_SECONDS` to `VERIFY_WINDOW_MAX_SECONDS` (default 60 to 1800); anything else is `invalid_argument`. When given, it enters `args` and `args_hash` like `override_manual_change`, so the approval pins it and apply must pass the same value (`plan_mismatch` otherwise), and after the consume the record's `verify_window_seconds` must equal it. When it is not given, the record's window is `VERIFY_WINDOW_DEFAULT_SECONDS` (120), which is not part of `args_hash`, and apply must not pass it either.
- `revert_on_failure` (scale only): a boolean, default `false` (any other type is `invalid_argument`). `true` asks the server to revert the change if its verification is `degraded` ([Revert](#revert)). It needs an explicit `verify_window_seconds`, so the human approving the apply sees both values rather than a server default; without one, and on a restart or rollback, it is `invalid_argument`. Like the window it enters `args` and `args_hash` only when true. When planning, after the blast-radius budget, the revert target (the current replica count) must be within the replica bounds (`out_of_bounds`, naming the revert target); `preflight` then lists `revert_target_in_bounds`, and the impact preview can warn `revert_baseline_unhealthy` ([Impact preview](impact.md#impact-preview)).

The plan response `data.verification` is `{"window_seconds": 60, "revert_on_failure": true, "revert_to": 1}`, where `revert_to`, the current replica count, appears only with `revert_on_failure`.

## Scheduling

The conditional update that consumes the plan ([Guard checks](../CONTRACT.md#guard-checks), apply step 3) also sets `verify_at = now + verify_window_seconds` and `verify_queue = "due"`, in the same write, so nothing after the patch has to succeed for the plan to be verified. A record without `verify_window_seconds` (planned before verification was deployed, or while it was off) is scheduled with `VERIFY_WINDOW_DEFAULT_SECONDS`. A plan whose apply is refused after the consume is scheduled too; its verification records `not_applied`. For a request with `revert_on_failure`, the same write copies `replicas_from` to `revert_to`: apply step 4 then confirms that it is the Deployment's current count, so it is the count the change replaced.

## The verifier

A scheduled Lambda (`mcp/verifier/`, EventBridge `rate(1 minute)`, the shortest rate a scheduled rule supports) outside any VPC, with reserved concurrency 1 and a 300-second timeout. It is not a gateway target. Each run:

1. queries `verify-due` for `verify_queue = "due" AND verify_at <= now`, longest due first, at most `VERIFY_BATCH_LIMIT` (10) plans; the rest wait for the next run;
2. invokes the write Lambda synchronously for each, with the payload `{"plan_id": "<id>"}` and the `ClientContext` `{"custom": {"bedrockAgentCoreToolName": "kh_internal___verify_applied_plan", "kubeHandrailChannel": "verifier", "kubeHandrailCorrelationId": "<the run's request ID>"}}`.

The verifier writes nothing itself: a failed invoke leaves the plan due for the next run. A check therefore runs up to a minute after `verify_at`, later when the queue is long, which is what `VERIFY_GRACE_SECONDS` absorbs.

Each run prints one JSON line, also a CloudWatch embedded metric record in `<name_prefix>/verifier` (no dimensions): `Runs` (1), `Due` (plans read), `Processed`, `InvokeErrors` (an invoke that failed, a function error or an error envelope), `Degraded` and `Reverts` (verifications that reported `degraded` with a final revert state, or reverted, so each plan counts once), plus `correlation_id` and `results`, one `{plan_id, outcome, reason, revert}` or `{plan_id, error}` per plan, where `error` is an exception class or an error code. Terraform creates these alarms on the alert topic, with `sentinel_enabled`:

- `<name_prefix>-verifier-stalled`: sum of `Runs` below 1 for 3 consecutive 5-minute periods, missing data breaching.
- `<name_prefix>-verifier-errors`: `AWS/Lambda` `Errors` of the verifier, sum over 5 minutes at least 1.
- `<name_prefix>-verifier-invoke-errors`: `InvokeErrors`, sum over 5 minutes greater than 0.
- `<name_prefix>-verifier-reverts`: `Reverts`, sum over 5 minutes greater than 0, so a human hears about every automatic revert.

## `verify_applied_plan(plan_id)`

The internal handler in the write Lambda ([Dispatch](../CONTRACT.md#dispatch)). The verification changes nothing in the cluster, so it reads no kill switch, runs no agent check and takes no budget unit; only a revert reads the kill switch. Every write it makes is conditional, so a run that repeats or overlaps an earlier one changes nothing.

1. Read the record consistently. If it does not exist, has no `verify_at`, `verify_at` is later than now, or it has no `verify_queue` (its verification is final; the index is eventually consistent and can still return it), return `ok` with `{"plan_id", "due": false}` and change nothing.
2. If it already has a `verification`, keep it and go to step 5.
3. Decide the outcome:
   - not consumed (`used = false`): `not_applied`;
   - now later than `verify_at + VERIFY_GRACE_SECONDS` (default 300): `unknown`, reason `late`, without reading anything: a verdict long after the approved window is not what was approved;
   - otherwise read the Deployment once (the writer's `get`). If the record's `result` is an error code and the Deployment's `kube-handrail.io/last-plan-id` does not name the plan, the apply was refused before its patch: `not_applied`. Otherwise evaluate the reading with the table below.
4. Write `verification = {outcome, reason, checked_at, observed, correlation_id}` with the condition `attribute_not_exists(verification)`. If another run wrote one first, that one is kept and used.
5. If the outcome is `degraded`, `args` has `revert_on_failure`, the record is not a revert record (`kind`) and it has no `revert` yet, revert ([Revert](#revert)). When the kill switch cannot be read, or the revert fails with an error, the run stops here and the plan stays due; past `verify_at + VERIFY_GRACE_SECONDS` any such failure records `revert = {status: skipped, reason: late}` instead.
6. Remove `verify_queue`, so the plan leaves the index.

The response `data` is `{"plan_id", "due": true, "outcome", "reason", "revert"}`, where `revert` is the revert's `status`, `pending` when it is retried on the next run, or `null`; only the verifier sees it.

| Outcome | Reason | Condition (the first match wins) |
|---|---|---|
| `unknown` | `not_found`, `kubernetes_error` | The read returned 404, or failed otherwise. |
| `unknown` | `replaced` | `metadata.uid` is not the plan's `uid`. |
| `unknown` | `superseded` | `kube-handrail.io/last-plan-id` does not name the plan: another apply or a human changed the Deployment since. |
| `unknown` | `spec_changed` | `metadata.generation` is not the plan's `generation + 1` (the apply bumps it exactly once), or `spec.replicas` (unset counts as 1) is not `replicas_to`. |
| `unknown` | `paused` | `spec.paused` is true: Kubernetes does not check the progress of a paused rollout. |
| `unknown` | `not_observed` | `status.observedGeneration` is below `metadata.generation`. |
| `degraded` | `progress_deadline_exceeded` | Condition `Progressing` is `False` with reason `ProgressDeadlineExceeded`. |
| `degraded` | `replica_failure` | Condition `ReplicaFailure` is `True`: the ReplicaSet controller could not create pods. |
| `degraded` | `target_not_reached` | The target below is not reached. |
| `healthy` | `target_reached` | Otherwise. |

The target: with `replicas_to` 0, `status.replicas` is 0. Otherwise condition `Available` is `True` and, for a change that keeps the pod template (a scale), `status.availableReplicas` is at least `replicas_to`; for one that rolls out a new template (a restart or rollback, whose `template_from` and `template_to` differ), as `kubectl rollout status` decides it: `status.updatedReplicas` is at least `replicas_to`, `status.replicas` is at most `status.updatedReplicas`, and `status.availableReplicas` is at least `status.updatedReplicas`. Unset counts are 0. An unchanged `generation + 1` also means the pod template is still the one the apply wrote (`template_to`).

The outcome answers one question: did this change reach its target within the approved window? It is one reading at `verify_at`, not a comparison with the state before the change: a Deployment that was already failing is `degraded` after a change that did not cause it, and a brief dip at the moment of the reading counts. Scaling counts as progress for `ProgressDeadlineExceeded` (default `progressDeadlineSeconds` 600), so with a shorter window a scale-up whose pods cannot start is normally `target_not_reached`.

`observed` is `{desired, replicas, ready, available, updated, generation, observed_generation, conditions}`, from the same reading: the counts, and each condition's `type`, `status` and `reason`, the reason cut to 256 characters. Condition messages are never stored.

## Revert

A revert undoes exactly one change that a human approved together with `revert_on_failure`, after its verification found it `degraded`. Its authority is that approval; it needs no approval of its own, and an `unknown` outcome never triggers it. The decision for each control ([ADR 0004](../../docs/adr/0004-pre-approved-revert.md)):

| Control | Stops a revert |
|---|---|
| Global kill switch | Yes |
| Replica bounds, GitOps ownership, the spec precondition below | Yes |
| The agent's own kill switch | No |
| Blast radius, cooldown, flapping, object, global and agent budgets, manual change | No |

1. **Already done, or late.** A record without `revert_to` (`revert_on_failure` that was not in the request the consume checked) is `refused` with reason `plan_mismatch`. The Deployment the verification read, or a fresh read on a later run (404: `refused` with reason `not_found`). If its `kube-handrail.io/last-plan-id` already names the revert record (below), an earlier run's patch landed and that run stopped before recording it: the revert is done, whatever changed since, and the revert record gets `result = applied` unless it has it. Otherwise, once `now` is later than `verify_at + VERIFY_GRACE_SECONDS`, `revert = {status: skipped, reason: late}`: a revert long after the approved window is not what was approved. Any error before the revert is recorded (an unreadable kill switch, a failed read or patch, a table error) records nothing and leaves the plan due, so the next run tries again until then.
2. **Kill switch.** The global kill switch, read without caching. Any value but `true`: `revert = {status: skipped, reason: remediation_disabled}`, final; turning the switch back on does not revert later, because the approved window has passed by then.
3. **Bounds.** The plan's `revert_to` must be within the replica bounds now: otherwise `refused` with reason `out_of_bounds`.
4. **Revert record.** A full plan record, created with one conditional `UpdateItem` (`attribute_not_exists(plan_id)`, so the write role needs no `PutItem`); if it exists, an earlier run created it and that one is used. `plan_id` is `uuid5(<fixed namespace>, "revert:" + parent plan_id)`, so a plan has at most one. `action` `scale_deployment`, `kind` `revert`, `reverts_plan_id`, the parent's `namespace`, `name`, `uid`, `object_key`, `resource_version`, `agent` and `requested_client_id`, `replicas_from = parent.replicas_to`, `replicas_to = parent.revert_to`, `generation = parent.generation + 1` (the spec the parent's apply left), `args` of the revert itself (`namespace`, `name`, `replicas`) with its `args_hash`, `template_from = template_to = ` the parent's `template_to`, `created_at = expires_at = now`, `retain_until`, `used = true`, `status = revert`, `requested_by = system:revert`, `channel = verifier`, `plan_correlation_id` (the verifier run), and the parent's window with `verify_at = now + window` and `verify_queue = "due"`. It never has `revert_on_failure`.
5. **Patch**, from the Deployment read in step 1. Its `kube-handrail.io/last-plan-id` must still name the parent (a re-read after a 409 that finds the revert record means a concurrent run's patch landed: the revert is done), and the [apply checks](../CONTRACT.md#guard-checks) of step 4 run against the revert record: the same `uid`, not GitOps-managed, `metadata.generation = parent.generation + 1` and `spec.replicas = parent.replicas_to`; a failure is `conflict` (with `details`) or `gitops_managed`. Then a server-side dry run and the merge patch `spec.replicas = parent.revert_to` with `metadata.resourceVersion` from that same read, `kube-handrail.io/last-plan-id` set to the revert record, `kube-handrail.io/correlation-id` to the verifier run's ID and `kube-handrail.io/requested-by` to `system:revert`. A 409 reads again, re-runs these checks and patches again, up to 3 attempts.
6. **Record.** On the revert record, `result` (`applied` or the code that stopped the patch), `applied_at`, `apply_correlation_id` and `applied_generation`. On the parent, with `attribute_not_exists(revert)`: `revert = {status: reverted, reason: null, revert_plan_id, at}`, or `{status: refused, reason: <code>, revert_plan_id, at}` when step 5 failed. A refusal is final.

The revert record is verified one window later like any applied plan and reports `healthy`, `degraded` or `unknown`. It is never reverted, for two independent reasons: it never has `revert_on_failure`, and step 5 of `verify_applied_plan` skips every record whose `kind` is `revert`. It counts in the history of later plans ([History checks](history-checks.md#history-checks-oscillation-breaker)).

## `get_plan_outcome(plan_id)`

`READ_ONLY` tool on `k8sr`. One consistent `GetItem` of the plan record; it never reads the cluster. `plan_id` must be a UUID (`invalid_argument`); an unknown plan is `plan_not_found`. A revert record can be read the same way, by its `revert_plan_id`.

```json
{
  "plan_id": "...", "cluster": "my-cluster", "action": "scale_deployment", "namespace": "sandbox", "name": "web",
  "kind": "change", "reverts_plan_id": null,
  "replicas": { "from": 1, "to": 2 },
  "approval_mode": "server",
  "approval": { "status": "approved", "approved_at": 1700000010 },
  "plan_status": "applied",
  "verification": {
    "window_seconds": 120, "verify_at": 1700000120,
    "status": "degraded", "reason": "target_not_reached", "checked_at": 1700000150,
    "observed": { "desired": 2, "replicas": 2, "ready": 1, "available": 1, "updated": 2,
                  "generation": 6, "observed_generation": 6,
                  "conditions": [ { "type": "Available", "status": "True",
                                    "reason": "<untrusted>MinimumReplicasAvailable</untrusted>" } ] }
  },
  "revert": { "requested": true, "status": "reverted", "reason": null, "revert_plan_id": "..." },
  "correlation_ids": { "plan": "...", "apply": "...", "verification": "..." }
}
```

- `plan_status` is the derived status of [Approval API](approval-api.md#approval-api) (`applied` once consumed).
- `verification.status` is the recorded outcome, with its `reason`, `checked_at` and `observed`, or, before there is one: `not_applied` (not consumed yet), `pending` (scheduled, and now is at most `verify_at + VERIFY_GRACE_SECONDS`), `unknown` with reason `check_overdue` (scheduled, and the check has not run within the grace period) or `unknown` with reason `not_scheduled` (consumed without a schedule: before verification was deployed, or while it was off). `checked_at` and `observed` are then `null`.
- `cluster` is the record's `cluster` (`null` on a record made before it).
- `kind` is `change`, or `revert` for a revert record, with `reverts_plan_id`.
- `approval_mode` is the record's, or `null`.
- `approval` is the record's `status` and `approved_at` (`null` when absent); in `both` mode agents wait here for `approved` (expiry: `plan_status`). It never names the approver.
- `revert` is `{"requested": false, "status": "not_requested"}` without `revert_on_failure` and on a revert record. With it, `{"requested": true, "status"}`: `pending` (no verdict yet, or `degraded` and not yet acted on), `not_needed` (the outcome is not `degraded`), or the recorded `reverted`, `skipped` or `refused` with its `reason` and `revert_plan_id`.
- `correlation_ids` holds those recorded, by step: `plan`, `apply`, `verification`.
- Condition reasons in `observed` are returned as [untrusted text](untrusted-output.md#untrusted-output): anyone who can write the Deployment's status sets them. A reason cut at its limit sets the envelope's `truncated`. Every other value was set by the server.

Known limits:

- Records made before verification have no schedule; `get_plan_outcome` reports them as `not_scheduled`.
- If the verifier cannot run within `VERIFY_GRACE_SECONDS` of `verify_at`, the outcome is `unknown` with reason `late`, and nothing is reverted.
- A revert of a restart or rollback is not supported: `revert_on_failure` is a scale argument only.
