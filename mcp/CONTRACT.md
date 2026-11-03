# Tool contract (Milestone 0)

Single source of truth for handlers, infrastructure code and tests. Change this file first.

## Targets and tools

| Gateway target | Lambda | Classification | Tools (schema file) |
|---|---|---|---|
| `k8sr` | `k8s_read` | `READ_ONLY` | `get_workload_status`, `plan_scale_deployment` (`mcp/k8s_read/tools.json`) |
| `k8sw` | `k8s_write` | `MUTATIVE` | `apply_scale_deployment` (`mcp/k8s_write/tools.json`) |

`plan_scale_deployment` and `apply_scale_deployment` take `namespace`, `name`, `replicas` and an optional boolean `override_manual_change` (default `false`; any other type is `invalid_argument`); apply also takes `plan_id`. See [History checks](#history-checks-oscillation-breaker).

Clients see `<target>___<tool>`, for example `k8sw___apply_scale_deployment`. Full names must be at most 64 characters and match `^[a-z0-9_]+$`.

## Dispatch

The tool name is the part after `___` in `context.client_context.custom["bedrockAgentCoreToolName"]`. The event is the tool arguments object. There is no `tool_name` argument and no fallback. An unknown tool returns error code `unknown_tool`.

## Response envelope

Every handler returns a JSON object (not wrapped in `statusCode`/`body`):

```json
{ "ok": true, "data": {}, "error": null, "truncated": false }
{ "ok": false, "data": null, "error": { "code": "plan_expired", "message": "..." }, "truncated": false }
```

`error` may also contain an optional `details` object with structured context. It is omitted when there is nothing to add. For `conflict` caused by a spec change since the plan was made:

```json
{
  "code": "conflict",
  "message": "Deployment spec changed since the plan was made",
  "details": {
    "generation": { "planned": 4, "current": 5 },
    "replicas": { "planned_from": 1, "current": 3 }
  }
}
```

`replicas.current` treats an unset `spec.replicas` as 1. For `conflict` because the Deployment was deleted and recreated since the plan was made, `details` is `{ "uid": { "planned": "...", "current": "..." } }`. A `conflict` from a 409 that persists after all patch attempts has no `details`. The history checks below also return `details`; every value in them is an integer or a string.

Error codes: `invalid_argument`, `unknown_tool`, `not_found`, `forbidden_namespace`, `out_of_bounds`, `gitops_managed`, `plan_not_found`, `plan_expired`, `plan_used`, `plan_mismatch`, `conflict`, `remediation_disabled`, `flapping`, `object_budget_exceeded`, `plan_rate_limited`, `manual_change`, `cooldown`, `global_budget_exceeded`, `kubernetes_error`, `internal`.

## Plan record (DynamoDB)

| Attribute | Type | Notes |
|---|---|---|
| `plan_id` | S | partition key, random UUID |
| `action` | S | `scale_deployment` |
| `namespace`, `name` | S | target |
| `replicas_from`, `replicas_to` | N | |
| `args_hash` | S | SHA-256 of `json.dumps({action, namespace, name, replicas}, sort_keys=True, separators=(",", ":"))`; the object gains `"override_manual_change": true` only when the override is true, so a hash without the override is the same as before the flag existed |
| `resource_version` | S | Deployment `metadata.resourceVersion` at plan time (informational) |
| `generation` | N | Deployment `metadata.generation` at plan time; a record without it is treated as `-1` and never matches |
| `uid` | S | Deployment `metadata.uid` at plan time; a record without it is treated as `""` and never matches |
| `object_key` | S | `<namespace>/<name>/<uid>`; partition key of the `object-history` index |
| `created_at` | N | epoch seconds; sort key of the `object-history` index |
| `expires_at` | N | epoch seconds, end of plan validity (`PLAN_TTL_SECONDS`, default 300 s) |
| `retain_until` | N | epoch seconds, the table TTL attribute: `created_at + max(3600, FLAP_WINDOW_SECONDS, PLAN_RATE_WINDOW_SECONDS, APPLY_COOLDOWN_SECONDS) + PLAN_TTL_SECONDS + 60`, so a record stays readable as history after the plan expired. A record without it is kept until deleted |
| `used` | BOOL | apply sets it with one conditional update: `used = false AND expires_at > now AND args_hash = :h AND action = :a` |
| `applied_at`, `result` | N, S | written by apply after the patch, on a best-effort basis: `result` is `applied`, or the error code that stopped the apply. An applied plan can therefore lack them; the Deployment's `kube-handrail.io/last-plan-id` annotation is the authoritative record of the last apply (see History checks) |

Global secondary index `object-history`: partition key `object_key` (S), sort key `created_at` (N), projection `ALL`. It is eventually consistent, so a plan or apply from the last moment may not be visible yet.

The table also holds one global budget counter per clock hour: `plan_id = "budget#global#<hour start, epoch seconds>"` (never a UUID, so it cannot be named as a plan), `applies` (N), and `retain_until` two hours after the hour starts. It has no `object_key`, so it is not in the index.

## Guard checks

Plan (`k8s_read`):
1. Arguments valid: namespace and name match DNS-1123; `replicas` integer.
2. Namespace in `ALLOWED_NAMESPACES`.
3. `MIN_REPLICAS <= replicas <= MAX_REPLICAS`.
4. Deployment exists and is not GitOps-managed. Fail closed: treat it as managed if it has the Argo CD `argocd.argoproj.io/tracking-id` annotation or the `app.kubernetes.io/instance` label (Argo CD label tracking; may also match Helm releases, which is acceptable).
5. Deployment has no HorizontalPodAutoscaler targeting it.
6. History checks: manual change, cooldown, flapping, object budget, then plan rate limit (below).
7. Global budget: the current hour's counter, read consistently, must be below `GLOBAL_HOURLY_APPLY_BUDGET`. Planning only reads it.

The plan response `data` has `plan_id`, `diff`, `override_manual_change` (the value pinned in `args_hash`), `preflight` and `expires_at`.

Apply (`k8s_write`), all re-checked independently of the plan step:
1. Kill switch: SSM parameter `REMEDIATION_ENABLED_PARAM` must equal `true`.
2. Checks 1-3 above (check 4 is re-run in step 4; check 5 is not, because the writer identity cannot list HPAs).
3. Plan exists, `action` matches, `args_hash` matches, not expired, not used. Mark used with a conditional update before patching.
4. Read the Deployment. Its `metadata.uid` must equal the planned `uid` (a Deployment deleted and recreated under the same name starts again at generation 1), it must not be GitOps-managed (check 4 above, re-run because a label-only change does not bump `generation`), and its `metadata.generation` must equal the planned `generation` and its `spec.replicas` (unset counts as 1) must equal `replicas_from`; otherwise return `conflict` with `details`, or `gitops_managed`. The API server bumps a Deployment's `generation` on spec and annotation changes but not on status or label updates, so a rollout in progress does not block the apply. Patch `spec.replicas` with a `metadata.resourceVersion` precondition taken from that read. On 409, read again, re-run these checks and patch again, up to 3 attempts in total; a change found on a retry returns the error immediately, and a 409 on the last attempt returns `conflict`.
5. Server-side dry run (`dryRun=All`) before each real patch. A plan is consumed before the dry run, so any failure after step 3 requires a new plan.
6. The patch also sets the annotation `kube-handrail.io/last-plan-id` to the applied `plan_id`.
7. History checks, after step 3 and after the first read in step 4 has passed: manual change, cooldown, flapping and object budget (not the plan rate limit), then the global budget: one conditional update adds 1 to the current hour's counter only if it is below `GLOBAL_HOURLY_APPLY_BUDGET`, so concurrent applies cannot overshoot it. The unit is taken before the patch and not returned if the patch then fails. They use the planned `replicas_from` and `replicas_to`; the check in step 4 has confirmed that `replicas_from` is the current count. A 409 retry re-runs step 4 but not the history checks. A rejection here leaves the plan consumed, with the error code as its `result`.

The kill switch is read on every apply (no caching), so turning it off takes effect immediately.

## History checks (oscillation breaker)

History is the set of plan records for one object, read through the `object-history` index for `created_at >= now - (max(3600, FLAP_WINDOW_SECONDS, PLAN_RATE_WINDOW_SECONDS, APPLY_COOLDOWN_SECONDS) + PLAN_TTL_SECONDS + 60)`. An object is `<namespace>/<name>/<uid>`, so a Deployment deleted and recreated under the same name starts with no history. An apply is a record with `result = "applied"`, ordered by `applied_at`; its direction is the sign of `replicas_to - replicas_from`. Windows exclude their start: an event exactly `W` seconds old is outside a window of `W` seconds. Any error reading the history or the last plan fails the request with `internal`; a check is never skipped.

The last apply is also taken from the Deployment itself: the plan named by its `kube-handrail.io/last-plan-id` annotation, read with a consistent `GetItem`, counts as applied when it is in the table, was consumed (`used = true`) and carries the Deployment's `uid`, whatever its recorded `result`. The annotation is set by the apply's own patch and read from the API server, so the most recent apply is counted even when the index has not caught up with it or its result was never recorded; when no `applied_at` was recorded, the latest time the apply can have happened, `min(expires_at, now)`, is used.

| Check | Rule | Code |
|---|---|---|
| Manual change | The Deployment has the annotation `kube-handrail.io/last-plan-id` (a UUID), that plan is in the table, was consumed (`used = true`; its recorded `result` is not required, because only a successful patch sets the annotation) and carries the Deployment's `uid`, and its `replicas_to` differs from the current replica count (unset counts as 1). Skipped when the annotation is absent or not a UUID, or the plan is no longer in the table or was never consumed; removing the annotation by hand therefore skips it too, which needs the same rights as scaling by hand. `override_manual_change: true` lets the plan or apply go ahead; the plan then reports the check with `"overridden": true`. | `manual_change` |
| Cooldown | The most recent apply is less than `APPLY_COOLDOWN_SECONDS` old. `0` turns the check off. | `cooldown` |
| Flapping | Among applies in the last `FLAP_WINDOW_SECONDS`, ignoring those that left the count unchanged, take the last two directions. Reject if the requested change reverses the last one and the last one reversed the one before it, i.e. a second reversal within the window. A single revert (up, then back down) is allowed. A request that keeps the count unchanged is never flapping. | `flapping` |
| Object budget | Applies in the last 3600 s `>= OBJECT_HOURLY_APPLY_BUDGET`. The apply being made is not counted. | `object_budget_exceeded` |
| Plan rate limit (plan only) | Plan records created in the last `PLAN_RATE_WINDOW_SECONDS` `>= PLAN_RATE_LIMIT`, whatever their result. Rejected plans are not stored and do not count. | `plan_rate_limited` |

`details`, with `history` listing up to the 20 most recent applies in the check's window, oldest first:

```json
{ "code": "flapping", "details": {
    "window_seconds": 600, "count": 2, "requested": { "from": 1, "to": 2 },
    "history": [ { "applied_at": 1700000000, "from": 1, "to": 2 }, { "applied_at": 1700000060, "from": 2, "to": 1 } ] } }
{ "code": "object_budget_exceeded", "details": { "window_seconds": 3600, "limit": 6, "count": 6, "history": [ ... ] } }
{ "code": "plan_rate_limited", "details": { "window_seconds": 600, "limit": 20, "count": 20, "history": [ ... ] } }
{ "code": "manual_change", "details": { "last_applied_to": 2, "current": 3 } }
{ "code": "cooldown", "details": { "cooldown_seconds": 60, "last_applied_at": 1700000060, "retry_after": 1700000120, "history": [ ... ] } }
{ "code": "global_budget_exceeded", "details": { "limit": 30, "window_start": 1699999200, "retry_after": 1700002800 } }
```

`count` is the number of applies in the window (flapping, budget) or of plans (rate limit).

`override_manual_change` is part of `args_hash`, so an approval pins it and apply must pass the same value as the plan (`plan_mismatch` otherwise). `false` and absent are the same.

Known limits:

- Two applies of different plans for one object cannot both succeed: the first patch bumps `generation` (an annotation change alone does), so the second fails apply step 4 with `conflict`, on its read or after a 409. Concurrent applies therefore cannot race the budget or flapping checks; concurrent applies of one plan are serialised by the single-use conditional update.
- The index is eventually consistent (normally well under a second). The last apply is always seen through the annotation; the apply before it is missed only if it is still absent from the index when the next plan and apply both run, which takes two complete plan/apply rounds inside the propagation delay.
- A request that keeps the count unchanged is allowed, has no direction, and once applied counts toward the budget.
- Rejected plans are not stored, so the plan rate limit does not bound requests refused by an earlier check. Each of those costs one Deployment read, one HPA list and two table reads, and changes nothing.
- Plans that are pending (created but not applied) do not count toward flapping, the cooldown or the budgets.
- The global budget counts clock hours, not a sliding window, so up to twice the limit can be applied across an hour boundary.
- The windows compare `applied_at` and `created_at`, written by one Lambda, with `now` on another; Lambda clocks are NTP-synchronised, so the skew is negligible against windows of minutes.

## Runtime and packaging

- Python 3.12, `x86_64`, pinned in one Terraform local.
- Dependencies installed by Terraform-driven build into `build/<function>/` with `pip install --target build/<function> --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12`, plus `mcp/common/`. `archive_file` zips `build/<function>/`.

## Identity

- Two IAM roles, one per Lambda. On the plan table: read role `dynamodb:PutItem` and `dynamodb:GetItem`; write role `dynamodb:GetItem` and `dynamodb:UpdateItem`; both `dynamodb:Query` on the `object-history` index ARN only.
- Two EKS access entries, type `STANDARD`, with `kubernetes_groups` `kube-handrail-reader` and `kube-handrail-writer`. No access policy associations.
- RBAC in the sandbox namespace only:
  - reader: `get`, `list` on `deployments`, `horizontalpodautoscalers`;
  - writer: `get`, `patch` on `deployments`.

## Configuration (environment variables)

`CLUSTER_NAME`, `PLANS_TABLE`, `ALLOWED_NAMESPACES` (comma-separated), `MIN_REPLICAS`, `MAX_REPLICAS`, `PLAN_TTL_SECONDS`, `REMEDIATION_ENABLED_PARAM` (write only). `AWS_REGION` is provided by Lambda.

History checks, set on both Lambdas; each must be a positive integer, except that the cooldown may be 0:

| Variable | Default | Terraform variable |
|---|---|---|
| `FLAP_WINDOW_SECONDS` | 600 | `flap_window_seconds` |
| `OBJECT_HOURLY_APPLY_BUDGET` | 6 | `object_hourly_apply_budget` |
| `PLAN_RATE_LIMIT` | 20 | `plan_rate_limit` |
| `PLAN_RATE_WINDOW_SECONDS` | 600 | `plan_rate_window_seconds` |
| `APPLY_COOLDOWN_SECONDS` | 60 (`0` turns it off) | `apply_cooldown_seconds` |
| `GLOBAL_HOURLY_APPLY_BUDGET` | 30 | `global_hourly_apply_budget` |
