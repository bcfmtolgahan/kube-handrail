# Tool contract

Single source of truth for handlers, infrastructure code and tests. Change this file first.

## Targets and tools

| Gateway target | Lambda | Classification | Tools (schema file) |
|---|---|---|---|
| `k8sr` | `k8s_read` | `READ_ONLY` | `get_workload_status`, `get_workload_events`, `plan_scale_deployment`, `request_approval` (`mcp/k8s_read/tools.json`) |
| `k8sw` | `k8s_write` | `MUTATIVE` | `apply_scale_deployment` (`mcp/k8s_write/tools.json`) |

`plan_scale_deployment` and `apply_scale_deployment` take `namespace`, `name`, `replicas` and an optional boolean `override_manual_change` (default `false`; any other type is `invalid_argument`); apply also takes `plan_id`. See [History checks](#history-checks-oscillation-breaker). `request_approval` takes only `plan_id`; see [Server-side approval](#server-side-approval). `get_workload_status` takes `namespace` and `name`; `get_workload_events` also takes an optional `limit`; see [Workload events](#workload-events).

Clients see `<target>___<tool>`, for example `k8sw___apply_scale_deployment`. Full names must be at most 64 characters and match `^[a-z0-9_]+$`.

The classification of every tool is in `mcp/classification.json`, a map from full tool name to `READ_ONLY` or `MUTATIVE` (`DESTRUCTIVE` is never used). It is the only place a classification is defined: `scripts/devops_agent_register.py` registers from it (`associate-read` the `READ_ONLY` tools; `enable-write` sets the association to exactly the classified tools, which also removes any tool added by hand), Terraform passes it to the drift sentinel, and `scripts/check_contract.py` (run in CI) fails unless:

- every tool in `mcp/*/tools.json` has a classification, and every classified tool exists;
- every tool of the write target `k8sw` (`mcp/k8s_write/tools.json`), and every `apply_*` tool on any target, is `MUTATIVE`. The sentinel and the registration script refuse a classification that breaks this rule as well, so a tampered `TOOL_CLASSIFICATION` cannot make a downgraded write tool look expected;
- every full name is at most 64 characters and matches `^[a-z0-9_]+$`;
- `terraform/main.tf` loads each `mcp/<function>/tools.json` under the target name used in the classification.

## Dispatch

The tool name is the part after `___` in `context.client_context.custom["bedrockAgentCoreToolName"]`. The event is the tool arguments object. There is no `tool_name` argument and no fallback. An unknown tool returns error code `unknown_tool`.

## Caller identity (experimental)

Without an interceptor, a tool Lambda learns nothing about the caller: `client_context.custom` carries the gateway, target, tool name and request IDs only. With `caller_interceptor_enabled = true`, Terraform attaches a gateway REQUEST interceptor (`mcp/interceptor/`) with `passRequestHeaders`. The gateway runs it after validating the inbound JWT. For `tools/call` it sets the reserved argument `_kube_handrail_caller` to the token's `client_id` (`unknown` if the token cannot be read), overwriting whatever the client sent under that name. It never logs or forwards the token.

Before any handler runs, dispatch removes `_kube_handrail_caller` from the arguments. It trusts the value only when the Lambda's `CALLER_INTERCEPTOR` is `true` (without the interceptor, a client could have sent it) and logs it as `caller` in the [tool record](#observability) (`null` when not trusted). It is not yet used for any decision: plans still record `requested_by = "mcp-gateway"`. Per-agent attribution and budgets are Milestone 2.

The reserved name is not in any tool's `inputSchema`. The gateway passed it through to the interceptor and on to the target without rejecting it.

## Observability

### Correlation ID

Dispatch takes each invocation's correlation ID from `context.client_context.custom`, using the first of these that is a string (or integer) matching `^[A-Za-z0-9._:-]{1,128}$`:

1. `bedrockAgentCoreAwsRequestId`, the gateway's ID for the request;
2. `kubeHandrailCorrelationId`, set only by the approver when it runs an apply (see [Approval API](#approval-api));

and otherwise the Lambda's own `aws_request_id`. A value that does not match is skipped, never logged. The gateway's `bedrockAgentCoreMcpMessageId` is not used: the client chooses the JSON-RPC message ID, so it is not unique across sessions. The approver is not behind the gateway; its correlation ID is its own Lambda request ID.

The ID is carried to:

- the tool record below, for every tool invocation;
- the plan record: `plan_correlation_id` (plan), `approval_request_correlation_id` (the `request_approval` call that notified), `decision_correlation_id` (the approver's request that approved or rejected) and `apply_correlation_id` (the apply that recorded its `result`, whether it succeeded or not). See [Plan record](#plan-record-dynamodb);
- the Deployment: the apply's patch sets the annotation `kube-handrail.io/correlation-id` to the apply's ID, next to `kube-handrail.io/last-plan-id`. Both are set by the same patch, which bumps `generation` once, as before;
- `data.correlation_id` in successful `plan_scale_deployment`, `request_approval` and `apply_scale_deployment` responses (`null` only outside a Lambda).

In `server` mode the approver passes its own request ID to the write Lambda as `kubeHandrailCorrelationId`, so the decision, the apply it runs, `apply_correlation_id` and the annotation share one ID, also when the apply fails.

The envelope itself is unchanged: error envelopes carry no correlation ID, because `error` and its `details` are a fixed contract that clients and tests match exactly. To find a refused call, search the tool records by `plan_id`, tool and time.

### Tool record

Every tool invocation, including `unknown_tool`, prints exactly one JSON line when it finishes. It is also a CloudWatch embedded metric format record in the namespace `METRIC_NAMESPACE` (Terraform: `<name_prefix>/tools`; `kube-handrail/tools` when unset):

```json
{
  "_aws": { "Timestamp": 1700000000000, "CloudWatchMetrics": [
    { "Namespace": "kube-handrail/tools", "Dimensions": [["Tool"], []],
      "Metrics": [ { "Name": "ToolCalls", "Unit": "Count" }, { "Name": "ToolErrors", "Unit": "Count" } ] },
    { "Namespace": "kube-handrail/tools", "Dimensions": [["Tool", "Code"]],
      "Metrics": [ { "Name": "GuardRejections", "Unit": "Count" } ] } ] },
  "Tool": "apply_scale_deployment", "Code": "conflict",
  "ToolCalls": 1, "ToolErrors": 0, "GuardRejections": 1,
  "tool": "apply_scale_deployment", "correlation_id": "...", "caller": null,
  "plan_id": "...", "outcome": "conflict", "duration_ms": 412
}
```

- `tool`: the tool name, `null` for an unknown tool (whose `Tool` dimension is `unknown`).
- `caller`: the verified caller ([Caller identity](#caller-identity-experimental)), otherwise `null`.
- `plan_id`: the `plan_id` of a successful response's `data`, otherwise the `plan_id` argument, and only if it is a UUID; otherwise `null`.
- `outcome`: `ok` or the error code.
- `ToolCalls` is 1. `ToolErrors` is 1 for `internal` and `kubernetes_error` (the tool could not do its job, for example the Kubernetes API refused its credentials or returned a server error). Any other error code is a guard rejection: the record then has `Code` and `GuardRejections = 1` and the second metric entry; an `ok`, `internal` or `kubernetes_error` record has neither. The empty dimension set gives `ToolCalls` and `ToolErrors` across all tools.

The record has only these fields. Argument values, tokens and error messages are never in it. An internal error additionally logs its stack trace through the Lambda logger, in a line that names the correlation ID.

Terraform creates these alarms on the drift sentinel's alert topic; they exist only with `sentinel_enabled`, which creates the topic:

- `<name_prefix>-tool-errors`: `ToolErrors` across all tools, sum over 5 minutes greater than 0, missing data not breaching.
- `<name_prefix>-k8s-read-errors` and `<name_prefix>-k8s-write-errors`: `AWS/Lambda` `Errors` of each tool Lambda, sum over 5 minutes at least 1. An invocation that times out, runs out of memory or fails to start prints no tool record, so only this alarm sees it.

The approver prints one JSON line per decision: `{"event": "decision", "plan_id", "decision", "approver", "correlation_id", "apply_outcome"}`, where `apply_outcome` is `ok`, the apply's error code, or `null` when no apply ran. The rejection reason is not logged.

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

Error codes: `invalid_argument`, `unknown_tool`, `not_found`, `forbidden_namespace`, `out_of_bounds`, `gitops_managed`, `plan_not_found`, `plan_expired`, `plan_used`, `plan_mismatch`, `conflict`, `remediation_disabled`, `flapping`, `object_budget_exceeded`, `plan_rate_limited`, `manual_change`, `cooldown`, `global_budget_exceeded`, `blast_radius_exceeded`, `approval_required`, `approval_rate_limited`, `plan_decided`, `forbidden`, `kubernetes_error`, `internal`.

## Untrusted output

Text that anyone with write access to the cluster can set, such as condition and event reasons and messages, is untrusted: it can carry instructions aimed at the agent reading it, secrets, or characters that hide what it says. Every such field is passed through `handrail_common.untrusted.untrusted_text` before it is returned, in this order:

1. **Control characters.** Characters of Unicode category `Cc` (C0 and C1 controls), except `\n` and `\t`, are removed, and so are all of category `Cf` (format characters: bidi embeddings, overrides and isolates U+202A-U+202E and U+2066-U+2069, zero-width characters U+200B-U+200F, U+2060, U+FEFF, soft hyphen, tag characters) and `Cs` (lone surrogates). They go first, so a zero-width character inside a secret cannot hide it from step 2.
2. **Secrets.** Each match is replaced by `[redacted:<kind>]`:

   | Kind | Matches |
   |---|---|
   | `private_key` | a PEM block from `-----BEGIN ... PRIVATE KEY-----` to its `END` line, or to the end of the text when the `END` line is missing |
   | `jwt` | `eyJ<base64url>.eyJ<base64url>.<base64url>` |
   | `bearer` | the token after `Bearer ` (case-insensitive), at least 8 characters; the word `Bearer` is kept |
   | `aws_access_key_id` | `AKIA` or `ASIA` followed by 16 upper-case letters or digits |
   | `aws_secret_access_key` | 40 base64 characters after a key name such as `aws_secret_access_key`, `secret_access_key` or `SecretAccessKey` and `=` or `:`; the key name is kept |
   | `github_token` | `ghp_`, `gho_`, `ghu_`, `ghs_` or `ghr_` followed by at least 36 letters or digits, and `github_pat_` followed by at least 22 |
   | `slack_token` | `xoxa-`, `xoxb-`, `xoxp-` or `xoxr-` followed by at least 10 letters, digits or dashes |
   | `credential` | the value in `<key>=<value>`, `<key>:<value>` (no space after the colon) or `"<key>": <value>`, where the key name ends in `password`, `passwd`, `secret`, `token` or `api_key` / `apikey` / `api-key` (case-insensitive); the key name is kept. The value is a quoted string or runs to the next space, quote, `,`, `;` or `&` |

   A `credential` value that is already a complete `[redacted:<kind>]` or `[truncated N chars]` marker is left as it is. Redaction is pattern-based and cannot find every secret; it is a backstop, not a reason to let secrets into events or logs. Redaction comes before truncation, so a secret cut in half at the limit is still recognised.
3. **Delimiters.** Each `<` followed by optional whitespace (including newlines), an optional `/`, optional whitespace and the word `untrusted` (case-insensitive), such as `</untrusted>` or `< UNTRUSTED >`, is replaced by `&lt;`, so the text cannot close its own wrapper or open a new one.
4. **Field limit.** A field longer than 1024 characters keeps its first characters and ends with the marker `[truncated N chars]`, where `N` is the number of characters removed; the result, marker included, is at most 1024 characters. The cut never falls inside a `[redacted:<kind>]` marker; it moves to the start of the marker instead.
5. **Wrapper.** The result is returned as the string `<untrusted>` + text + `</untrusted>`. A field that is `null` stays `null`.

Steps 1-4 are idempotent: applying them to their own output changes nothing. Step 5 is not, and is not meant to be: text shaped like a wrapped value is neutralized and wrapped again, never passed through.

The `get_workload_events` response is limited to 32768 bytes of serialized `data` (`json.dumps`, which escapes non-ASCII characters, so the bytes on the wire are no more). Whole events are dropped from the end of the list, the oldest first, until it fits; the response then has `truncated: true` in `data` and in the envelope. A field cut at its limit also sets the envelope's `truncated`. `get_workload_status` needs no response limit: a Deployment has a handful of condition types, each with two fields of at most 1024 characters.

The delimiters tell a model which text did not come from kube-handrail; they are a hint, not a security boundary, and a model may still follow instructions inside them. The boundary is elsewhere: no `READ_ONLY` tool can change the cluster, and every change needs a plan whose exact arguments a human approves (in the client, in `server` mode on the server, or both). Text inside a tool response can at most make an agent propose a plan, which the approver sees with the server's own record of its arguments and impact.

## Workload events

`get_workload_events(namespace, name, limit?)` returns recent Kubernetes Events about a Deployment and the objects it owns. `namespace` and `name` are validated and the namespace checked against `ALLOWED_NAMESPACES` as for the other tools; `limit` is an integer from 1 to 50 (default 20; anything else is `invalid_argument`).

1. Read the Deployment (`not_found` if it does not exist).
2. List the ReplicaSets in the namespace (`apps/v1`) and keep those with an `ownerReferences` entry whose `uid` is the Deployment's `uid`.
3. List the Events in the namespace (`core/v1`). An Event is kept when its `involvedObject.namespace` is absent or the namespace, its `involvedObject.name` is a DNS-1123 subdomain, and it is about:
   - the Deployment: `kind` `Deployment` and the Deployment's name;
   - one of the ReplicaSets from step 2: `kind` `ReplicaSet` and its name;
   - a Pod of one of them: `kind` `Pod` and a name that is the ReplicaSet's name plus `-`, cut to 58 characters as the API server does for generated names, followed by exactly 5 lower-case letters or digits. Pods are matched by name only; the reader cannot read Pods.

   When `involvedObject.uid` is set for a Deployment or ReplicaSet, it must be that object's `uid`, so the events of a deleted object with the same name are left out.

   Only ReplicaSets that still exist are matched. Events about a ReplicaSet already removed by the Deployment's `revisionHistoryLimit`, and about its Pods, are left out although `kubectl get events` still shows them, so an empty list does not mean no failure.
4. Sort newest first by the first set of `lastTimestamp`, `series.lastObservedTime`, `eventTime`, `firstTimestamp` and `metadata.creationTimestamp`; events without any valid timestamp go last; ties are broken by event name. Keep the first `limit`, then apply the response limit from [Untrusted output](#untrusted-output).

Both lists are read as raw JSON (`_preload_content=False`), like the impact preview. A failed read of either is `kubernetes_error`, never an empty list.

```json
{
  "namespace": "sandbox",
  "name": "web",
  "events": [
    {
      "type": "<untrusted>Normal</untrusted>",
      "reason": "<untrusted>ScalingReplicaSet</untrusted>",
      "message": "<untrusted>Scaled up replica set web-5d4f8c7b9 to 2</untrusted>",
      "object": { "kind": "Deployment", "name": "web" },
      "count": 1,
      "first_timestamp": "2026-01-01T12:00:00Z",
      "last_timestamp": "2026-01-01T12:00:00Z"
    }
  ],
  "matched": 1,
  "truncated": false
}
```

`type`, `reason` and `message` are untrusted text. `object` is the matched object, so it holds only names kube-handrail derived itself. `count` is `count`, else `series.count`, else 1. `first_timestamp` is `firstTimestamp`, else `eventTime`; `last_timestamp` is the timestamp the event was sorted by; each is the API server's RFC 3339 string, or `null` when absent or not a valid timestamp. `matched` is the number of Events kept in step 3, before `limit` and the response limit.

`get_workload_status` passes each condition's `reason` and `message` through the same function; its `data` is otherwise unchanged.

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
| `expires_at` | N | epoch seconds, end of plan validity: `created_at + PLAN_TTL_SECONDS` (default 300 s); `created_at + APPROVAL_WAIT_TTL_SECONDS` once an approval is requested; `approved_at + APPLY_AFTER_APPROVAL_TTL_SECONDS` once approved; `rejected_at` once rejected |
| `retain_until` | N | epoch seconds, the table TTL attribute: `created_at + max(3600, FLAP_WINDOW_SECONDS, PLAN_RATE_WINDOW_SECONDS, APPLY_COOLDOWN_SECONDS) + max(PLAN_TTL_SECONDS, APPROVAL_WAIT_TTL_SECONDS) + APPLY_AFTER_APPROVAL_TTL_SECONDS + 60`, the longest window plus the longest a plan can stay valid, so a record stays readable as history after the plan expired. A record without it is kept until deleted |
| `used` | BOOL | apply sets it with one conditional update: `used = false AND expires_at > now AND args_hash = :h AND action = :a AND attribute_not_exists(rejected_by)`, extended in `server` and `both` mode (see [Server-side approval](#server-side-approval)) |
| `requested_by` | S | the requesting principal. Until per-agent identity reaches the server it is the constant `mcp-gateway`, the class of the gateway's machine identities |
| `status` | S | `planned`, `awaiting_approval`, `approved` or `rejected`. `applied` (`used = true`) and `expired` (`expires_at <= now`) are derived, never stored. Records made before server approval have no `status` and `requested_by`; they can be applied in `client` mode until they expire but never requested or approved |
| `override_manual_change` | BOOL | the value pinned in `args_hash` |
| `impact` | M | the impact preview returned with the plan, so the approval channel renders the server's own record |
| `approval_requested_at` | N | last `request_approval` that notified |
| `approved_by`, `approved_at`, `approved_args_hash`, `approval_channel`, `approval_expires_at` | S, N, S, S, N | written by an approval: the approver's IAM principal ARN as it signed the request, the time, the plan's own `args_hash` copied in the same update, `api`, and `approved_at + APPLY_AFTER_APPROVAL_TTL_SECONDS` |
| `rejected_by`, `rejected_at`, `reject_reason` | S, N, S | written by a rejection; the reason is at most 500 characters with control characters removed |
| `applied_at`, `result`, `apply_correlation_id` | N, S, S | written by apply after the patch, on a best-effort basis: `result` is `applied`, or the error code that stopped the apply; `apply_correlation_id` is the apply's correlation ID. An applied plan can therefore lack them; the Deployment's `kube-handrail.io/last-plan-id` annotation is the authoritative record of the last apply (see History checks) |
| `plan_correlation_id`, `approval_request_correlation_id`, `decision_correlation_id` | S | the correlation IDs of the plan, of the last `request_approval` that notified, and of the approval or rejection ([Observability](#observability)). Each is absent until its step happens; records made before them have none |

Global secondary index `object-history`: partition key `object_key` (S), sort key `created_at` (N), projection `ALL`. It is eventually consistent, so a plan or apply from the last moment may not be visible yet.

The table also holds one global budget counter per clock hour: `plan_id = "budget#global#<hour start, epoch seconds>"` (never a UUID, so it cannot be named as a plan), `applies` (N), and `retain_until` two hours after the hour starts; and one approval request counter per 600-second clock window: `plan_id = "approval#global#<window start>"`, `requests` (N), `retain_until` two windows after the window starts. They have no `object_key`, so they are not in the index.

## Guard checks

Plan (`k8s_read`):
1. Arguments valid: namespace and name match DNS-1123; `replicas` integer.
2. Namespace in `ALLOWED_NAMESPACES`.
3. `MIN_REPLICAS <= replicas <= MAX_REPLICAS`.
4. Deployment exists and is not GitOps-managed. Fail closed: treat it as managed if it has the Argo CD `argocd.argoproj.io/tracking-id` annotation or the `app.kubernetes.io/instance` label (Argo CD label tracking; may also match Helm releases, which is acceptable).
5. Deployment has no HorizontalPodAutoscaler targeting it; otherwise `conflict` without `details`.
6. Blast-radius budget: `|replicas - current| <= max(1, ceil(current * MAX_REPLICA_STEP_PERCENT / 100))`, where `current` is `spec.replicas` (unset counts as 1); otherwise `blast_radius_exceeded`. It is enforced whatever the approval says.
7. History checks: manual change, cooldown, flapping, object budget, then plan rate limit (below).
8. Global budget: the current hour's counter, read consistently, must be below `GLOBAL_HOURLY_APPLY_BUDGET`. Planning only reads it.
9. Impact preview: list the PodDisruptionBudgets, Services and EndpointSlices in the namespace (below). Any error fails the plan with `kubernetes_error`, so the approver never sees a preview with gaps. Only then is the plan record written.

The plan response `data` has `plan_id`, `diff`, `override_manual_change` (the value pinned in `args_hash`), `preflight`, `impact`, `expires_at` and `correlation_id`. `preflight` lists the checks that passed, in this order: `arguments`, `namespace_allowed`, `replica_bounds`, `not_gitops_managed`, `no_hpa`, `blast_radius`, `no_manual_change`, `cooldown`, `not_flapping`, `object_budget`, `plan_rate`, `global_budget`.

`blast_radius_exceeded` `details`:

```json
{ "code": "blast_radius_exceeded", "details": { "from": 1, "to": 3, "max_step": 1, "max_step_percent": 100 } }
```

With the default of 100 %, a Deployment at 0 or 1 replica can change by 1, at 3 by 3 (to anything from 0 to 6); at 50 %, a Deployment at 3 can change by 2.

Apply (`k8s_write`), all re-checked independently of the plan step:
1. Kill switch: SSM parameter `REMEDIATION_ENABLED_PARAM` must equal `true`.
2. Checks 1-3 above (check 4 is re-run in step 4, check 6 in step 3; check 5 is not, because the writer identity cannot list HPAs, and the impact preview is not, because it only informs the approver).
3. Plan exists, `action` matches, `args_hash` matches, not expired, not used, and in `server` and `both` mode approved (see [Server-side approval](#server-side-approval)). Mark used with a conditional update before patching. Then the blast-radius budget (plan check 6), with the plan's `replicas_from` and `replicas_to` and the current `MAX_REPLICA_STEP_PERCENT`, so lowering the setting also stops plans made before the change. A rejection leaves the plan consumed with `result = "blast_radius_exceeded"`.
4. Read the Deployment. Its `metadata.uid` must equal the planned `uid` (a Deployment deleted and recreated under the same name starts again at generation 1), it must not be GitOps-managed (check 4 above, re-run because a label-only change does not bump `generation`), and its `metadata.generation` must equal the planned `generation` and its `spec.replicas` (unset counts as 1) must equal `replicas_from`; otherwise return `conflict` with `details`, or `gitops_managed`. The API server bumps a Deployment's `generation` on spec and annotation changes but not on status or label updates, so a rollout in progress does not block the apply. Patch `spec.replicas` with a `metadata.resourceVersion` precondition taken from that read. On 409, read again, re-run these checks and patch again, up to 3 attempts in total; a change found on a retry returns the error immediately, and a 409 on the last attempt returns `conflict`.
5. Server-side dry run (`dryRun=All`) before each real patch. A plan is consumed before the dry run, so any failure after step 3 requires a new plan.
6. The patch also sets the annotations `kube-handrail.io/last-plan-id` to the applied `plan_id` and `kube-handrail.io/correlation-id` to the apply's [correlation ID](#observability).
7. History checks, after step 3 and after the first read in step 4 has passed: manual change, cooldown, flapping and object budget (not the plan rate limit), then the global budget: one conditional update adds 1 to the current hour's counter only if it is below `GLOBAL_HOURLY_APPLY_BUDGET`, so concurrent applies cannot overshoot it. The unit is taken before the patch and not returned if the patch then fails. They use the planned `replicas_from` and `replicas_to`; the check in step 4 has confirmed that `replicas_from` is the current count. A 409 retry re-runs step 4 but not the history checks. A rejection here leaves the plan consumed, with the error code as its `result`.

The kill switch is read on every apply (no caching), so turning it off takes effect immediately.

## Impact preview

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
  - `scale_to_zero`: `to` is 0.
  - `pdb_headroom`: `removed` is greater than `disruptions_allowed` of a matching PodDisruptionBudget (unset counts as 0). A scale-down is not an eviction, so the PodDisruptionBudget will not stop it.
  - `service_without_endpoints`: a matching Service's `projected_ready_endpoints` is 0.

`truncated` is true when any of the three lists had more than 20 entries; each list then holds the first 20 (sorted), warnings having been computed over all of them, and the envelope's `truncated` is true as well. An object the preview cannot interpret, such as a selector with an unknown operator, fails the plan with `kubernetes_error`.

The lists are read as raw JSON (`_preload_content=False`), because the Kubernetes Python client refuses some valid objects, such as the placeholder EndpointSlice with `endpoints: null` of a Service without pods.

## History checks (oscillation breaker)

History is the set of plan records for one object, read through the `object-history` index for `created_at >= now - (max(3600, FLAP_WINDOW_SECONDS, PLAN_RATE_WINDOW_SECONDS, APPLY_COOLDOWN_SECONDS) + max(PLAN_TTL_SECONDS, APPROVAL_WAIT_TTL_SECONDS) + APPLY_AFTER_APPROVAL_TTL_SECONDS + 60)`, the same span as `retain_until`. An object is `<namespace>/<name>/<uid>`, so a Deployment deleted and recreated under the same name starts with no history. An apply is a record with `result = "applied"`, ordered by `applied_at`; its direction is the sign of `replicas_to - replicas_from`. Windows exclude their start: an event exactly `W` seconds old is outside a window of `W` seconds. Any error reading the history or the last plan fails the request with `internal`; a check is never skipped.

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

## Drift sentinel

A write tool that loses its `MUTATIVE` classification runs without approval in DevOps Agent (an unclassified tool counts as `READ_ONLY`). The sentinel is a scheduled Lambda (`mcp/sentinel/`, every 5 minutes by default) that turns remediation off when that happens. It is not a gateway target and has no MCP tools.

Each run checks, against `mcp/classification.json`:

1. **Gateway.** `ListGatewayTargets`, then `GetGatewayTarget` for each expected target: the tool names in the target's inline tool schema must be exactly the classified tools of that target. A missing or extra tool, a missing target, or a target that is not in the classification is drift.
2. **DevOps Agent** (only when an agent space and association are configured). `GetAssociation`: the associated `mcpserver` tools must be exactly the classified tools, and every `MUTATIVE` tool must be associated as `MUTATIVE`; a tool without a `toolDetails` entry counts as `READ_ONLY`. A `READ_ONLY` tool associated as `MUTATIVE` only adds an approval and is not drift. `GetAgentSpace`: `elevatedActionsEnabled` (absent counts as `false`) must equal the configured expectation. Configure the IDs only after `enable-write`: before it, the write tool is not associated, which is reported as drift.

The gateway, association and agent-space checks run independently. Findings from one are acted on even if another fails, because the only action is turning remediation off; a run with errors and no findings is `unknown`.

Outcomes, returned and logged as one JSON line `{ "status": "ok" | "drift" | "unknown", "findings": [ { "check", "issue", "subject", ... } ], "errors": [ "<Operation>: <ErrorCode>" ] }` that is also a CloudWatch embedded metric record (namespace `<name_prefix>/sentinel`, metrics `Drift` and `Unknown`, 0 or 1 per run):

| Status | When | Action |
|---|---|---|
| `ok` | No finding. | None. |
| `drift` | At least one finding (issues `missing_tool`, `extra_tool`, `missing_target`, `extra_target`, `not_mutative`, `elevated_actions`). | Set the kill switch parameter to `false` (skipped if it already is), then publish an alert to the SNS topic listing the findings by tool name only. If either step fails, the run still does the other, then fails, so it is retried and the error alarm fires. |
| `unknown` | Any API error (including throttling and missing permissions), a response that cannot be read, a gateway target that is not `READY`, or invalid configuration. | Log the error code and operation (not the message), emit `Unknown = 1`. The kill switch is never touched and no drift alert is sent. A CloudWatch alarm notifies the same topic after 3 consecutive 5-minute periods with an unknown result or with no run at all (missing data counts as breaching). |

The sentinel only ever writes `false`. Restoring remediation is manual:

1. Find the cause in the alert and fix it, for example a registration update that dropped `toolDetails`.
2. Re-run `scripts/devops_agent_register.py enable-write`, which reclassifies from `mcp/classification.json` and exits non-zero unless the write tools are `MUTATIVE` and elevated actions are on. For gateway drift, re-apply Terraform.
3. Wait for a sentinel run to report `ok` (or invoke the function), then set the kill switch parameter back to `true`: `aws ssm put-parameter --overwrite --name <kill_switch_parameter> --value true`.

Configuration (environment variables, set by Terraform): `GATEWAY_ID`, `REMEDIATION_ENABLED_PARAM` (the write Lambda's kill switch), `ALERT_TOPIC_ARN`, `METRIC_NAMESPACE`, `TOOL_CLASSIFICATION` (the content of `mcp/classification.json`), `AGENT_SPACE_ID` and `ASSOCIATION_ID` (both or neither; empty skips the DevOps Agent checks), `EXPECT_ELEVATED_ACTIONS` (`true` or `false`, default `true`). Terraform variables: `sentinel_enabled`, `sentinel_schedule`, `devops_agent_space_id`, `devops_agent_association_id`, `expect_elevated_actions`, `alert_email`.

Known limits:

- A Terraform apply that changes the tool set updates the gateway targets before the sentinel's own configuration; a run in between reports drift and turns remediation off.
- Detection takes up to one schedule interval; until then a misclassified write tool runs without approval, within the server-side limits.
- The sentinel sees what the control-plane APIs report, not what a running DevOps Agent chat has cached (a chat keeps the tool list it was created with).
- It watches one association in one agent space. Another association of the same gateway, in this or another agent space, is not checked; in `server` or `both` mode ([Server-side approval](#server-side-approval)) a misclassified write tool still cannot apply without a server approval.
- A deploy that adds a tool (such as `request_approval`) is drift for the association check until `enable-write` has added it to the association: with the DevOps Agent IDs configured, run `enable-write` right after `terraform apply`, wait for an `ok` run, then set the kill switch back to `true`.
- It compares tool names, not the Lambda function behind a target. Changing a target's function needs gateway write access, which is outside what classification drift covers.

## Server-side approval

Implements [ADR 0002](../docs/adr/0002-server-verified-approval.md). A human approval is a fact stored in the plan record and checked by the apply's own conditional write, instead of a step the server trusts the client to have done.

### Modes

`APPROVAL_MODE` (Terraform `approval_mode`, default `server`; an unset variable also means `server`, so a missing setting fails closed):

| Mode | Apply requires a server approval | Use |
|---|---|---|
| `client` | No; the condition is the one from Milestone 0. | DevOps Agent chat: the chat approval is the only one. |
| `server` | Yes. | Every other client, and DevOps Agent autonomous investigations (plan and `request_approval`, then a human approves through the approval channel, which also runs the apply). |
| `both` | Yes. The server cannot see the client's approval, so the apply condition is the one of `server`. The approval channel never runs the apply in this mode: the client applies, after its own approval. | DevOps Agent chat with a server approval too: the agent plans and calls `request_approval`, a human approves through the approval channel, then the agent applies in chat and the chat approval is asked as usual. |

Only in `server` mode does the approval channel run the apply. `request_approval` is refused with `invalid_argument` in `client` mode, where the client approves and a server approval would change nothing. The approval API works in every mode; in `client` mode an approval is recorded but not required.

### Lifecycle

```
planned -> awaiting_approval -> approved -> applied
   |               |               +-> expired (APPLY_AFTER_APPROVAL_TTL_SECONDS)
   |               +-> rejected
   |               +-> expired (APPROVAL_WAIT_TTL_SECONDS from created_at)
   +-> approved, rejected, expired (PLAN_TTL_SECONDS)
```

A plan can be approved without a request (for example right after planning). Every decision is final: an approved or rejected plan cannot be decided again or re-requested (`plan_decided`, `details: {"status": "approved" | "rejected"}`). A rejection also sets `expires_at` to the rejection time, so no mode can apply a rejected plan.

### Apply condition

In every mode the consume condition refuses a rejected plan (`attribute_not_exists(rejected_by)`), even if the rejecting and the applying clock disagree about `expires_at`. In `server` and `both` mode it becomes

```
used = false AND expires_at > :now AND args_hash = :h AND action = :a
AND attribute_not_exists(rejected_by)
AND status = "approved" AND attribute_exists(approved_by)
AND approved_by <> requested_by AND approved_args_hash = args_hash
AND approval_expires_at > :now
```

Approval, argument match, separation of duties, expiry and single use are one atomic write. When it fails, the record is read again and the first matching reason is returned:

1. no record: `plan_not_found`; `used`: `plan_used`;
2. `status = rejected` (every mode): `approval_required`, `details: {"status": "rejected"}`;
3. approved but `approval_expires_at <= now` (server and both): `approval_required`, `details: {"status": "approval_expired"}`;
4. `expires_at <= now`: `plan_expired`;
5. arguments differ: `plan_mismatch` (the approval is pinned to `args_hash`, including `override_manual_change`);
6. not approved (server and both): `approval_required`, `details: {"status": "planned" | "awaiting_approval"}`;
7. approved by the requester, or an approval of other arguments (only possible by writing the table directly): `approval_required`, `details: {"status": "approval_invalid"}`.

After the consume, the record's `namespace`, `name` and `replicas_to` must equal the arguments (`plan_mismatch` otherwise), so a record altered in the table cannot redirect an apply even with a matching `args_hash`. A successful apply's `data` is `plan_id`, `namespace`, `name`, `replicas_from`, `replicas_to`, `resource_version`, `approved: true | false` and `correlation_id`; the approver's identity stays in the plan record and the logs. Everything after the consume (blast radius, live object checks, history checks, global budget, dry run) is unchanged: an approval never lifts a guard.

### `request_approval(plan_id)`

`READ_ONLY` tool on `k8sr`. It approves nothing and only `plan_id` is read from the arguments, so no agent text reaches the approvers.

1. `plan_id` must be a UUID, and the mode must be `server` or `both` (`invalid_argument` otherwise, so a plan's life is never extended in `client` mode). Without a configured `APPROVAL_TOPIC_ARN` the call fails with `internal` before the plan changes.
2. The record is read consistently: `plan_not_found`, `plan_used`, `plan_decided` (approved or rejected), `plan_expired`.
3. If it already notified less than `APPROVAL_REQUEST_COOLDOWN_SECONDS` ago, nothing changes and nobody is notified: `ok` with `notified: false` and `retry_after`.
4. One conditional update adds 1 to the current window's approval request counter only while it is below `APPROVAL_REQUEST_LIMIT` (`approval_rate_limited`, `details: {"limit", "window_seconds": 600, "window_start", "retry_after"}`). Clock windows, so up to twice the limit can pass across a window boundary.
5. One conditional update sets `status = awaiting_approval`, `approval_requested_at = now`, `approval_request_correlation_id` and `expires_at = created_at + APPROVAL_WAIT_TTL_SECONDS` (from creation, so asking again never extends a plan). If the record changed between the read and this update and the cooldown does not explain it, the call fails with `conflict` ("plan changed concurrently; try again"); calling it again is safe.
6. The notification is published to the server's SNS topic, rendered from the record: Deployment, replica change, override flag, requester, expiry, the impact summary and warnings, and the `scripts/approve.py` commands with the approval URL. The agent cannot choose or add a destination. Publishing happens after the update; if it fails, the call fails with `internal`, the plan stays awaiting approval and the next notification is possible after the cooldown.

Response `data`: `plan_id`, `status` (`awaiting_approval`), `expires_at`, `notified`, `approval_mode`, `correlation_id`, and `retry_after` when `notified` is false.

### Approval API

The approver Lambda (`mcp/approver/`) is not a gateway target and has no MCP tools. It is served through a Lambda Function URL with `AuthType AWS_IAM`, so every request is SigV4-signed (service `lambda`) by an IAM principal; a bearer token, such as a Cognito token, is refused by the URL itself with 403. The caller is `requestContext.authorizer.iam.userArn`.

Authorization, before anything else:

1. A request without an IAM identity is `forbidden`.
2. A caller matching `DENIED_PRINCIPALS` (the roles of the read, write, sentinel and approver Lambdas and of the gateway, set by Terraform) is `forbidden`, even if it is also listed as an approver: machine identities never approve.
3. The caller must match `APPROVER_PRINCIPALS`: an IAM user ARN matches exactly that ARN (including its path); an IAM role ARN `arn:aws:iam::<account>:role[/path]/<name>` matches its sessions `arn:aws:sts::<account>:assumed-role/<name>/<any session>`. Otherwise `forbidden`.

Requests (responses are the JSON envelope in the body):

- `GET /?plan_id=<uuid>`: the plan rendered from the record: `plan_id`, `status` (derived: `planned`, `awaiting_approval`, `approved`, `rejected`, `applied`, `expired`), `action`, `namespace`, `name`, `replicas: {from, to}`, `override_manual_change`, `impact`, `requested_by`, `created_at`, `expires_at`, and when present `approval`, `rejection`, `result` and `correlation_ids` (the record's correlation IDs by step: `plan`, `approval_request`, `decision`, `apply`, each only when recorded).
- `POST /` with a JSON object of at most 4096 bytes and only these fields: `plan_id` (UUID), `decision` (`approve` or `reject`), optional `reason` (string, at most 500 characters, control characters removed), optional `apply` (boolean, default `true`). Any other field is `invalid_argument`.
  - `approve`: one conditional update, `status IN (planned, awaiting_approval) AND used = false AND expires_at > now AND requested_by <> caller`, sets `status = approved`, `approved_by`, `approved_at`, `approved_args_hash = args_hash` (copied from the record in the same update), `approval_channel = api`, `approval_expires_at = expires_at = now + APPLY_AFTER_APPROVAL_TTL_SECONDS` and `decision_correlation_id` (the approver's request ID).
  - `reject`: the same condition; sets `status = rejected`, `rejected_by`, `rejected_at`, `reject_reason`, `decision_correlation_id`, `expires_at = now`.
  - Refusals: `plan_not_found`, `plan_used`, `plan_decided`, `plan_expired`, `forbidden` (the requester), `conflict` (changed concurrently).
  - In `server` mode only, after a successful approval with `apply` true, the approver invokes the write Lambda synchronously with the plan's own arguments from the record (`plan_id`, `namespace`, `name`, `replicas = replicas_to`, and `override_manual_change: true` only when the plan has it) and `ClientContext` `{"custom": {"bedrockAgentCoreToolName": "k8sw___apply_scale_deployment", "kubeHandrailCorrelationId": "<approver request ID>"}}`, as the gateway calls it except that the correlation ID is the approver's ([Observability](#observability)). The write guard is unchanged and authoritative.
  - Response `data`: `plan` (the view above, read again after the apply), `apply` (the write Lambda's envelope, or `null` when no apply ran, as always in `client` and `both` mode) and `approval_mode`. If the write Lambda cannot be invoked at all, `apply` is `internal` with the message `approval recorded; apply could not be started: <error code>`. A failed apply does not undo the approval: an approval whose apply was refused before the consume (for example `remediation_disabled`) can still be applied through the gateway until it expires.
- Any other method: `invalid_argument`.

HTTP status: 200 for `ok` (also when the triggered apply failed; see `data.apply`), 400 `invalid_argument`, 403 `forbidden`, 404 `plan_not_found`, 409 `plan_used`, `plan_expired`, `plan_decided`, `conflict`, 500 otherwise. Each decision is logged as one JSON line with the caller ARN, `plan_id` and correlation ID ([Observability](#observability)); each `forbidden` refusal is logged with the caller ARN.

`scripts/approve.py` (`show`, `approve [--no-apply]`, `reject [--reason]`; `--url` or `APPROVAL_URL`) signs with the operator's own AWS credentials, prints the server-rendered plan and asks for confirmation unless `--yes`. When an approval ran no apply, it says that the client must now apply the plan.

### MFA and who can reach the URL

A Lambda resource policy cannot carry an `aws:MultiFactorAuthPresent` condition (AddPermission takes no conditions). Calling an `AWS_IAM` function URL takes `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` (the latter with `lambda:InvokedViaFunctionUrl`). Therefore:

- `approval_require_mfa = true` (default): Terraform creates the IAM policy in the `approver_policy_arn` output, which grants both actions on the approver function only with MFA present. Attach it to each approver user or role (for a role, require MFA in its trust policy as well). The function's resource policy grants nobody, because a same-account allow there would bypass the MFA condition.
- `approval_require_mfa = false` (test setups only, for example an IAM user with access keys and no MFA session): the resource policy grants each listed principal both actions, without MFA.

Either way the allowlist in the approver's code is the gate that counts. Any same-account principal whose own policies allow `lambda:InvokeFunctionUrl` and `lambda:InvokeFunction` broadly (for example administrator access) reaches the URL without MFA and is stopped only by the allowlist. Grant approvers access only through the policy above. Anyone who can invoke the approver function directly (outside the URL, with a forged event) or write the plan table can forge an approval; both are administrator-level rights and should be kept that way.

Known limits:

- Separation of duties is between principal classes: every plan is requested by `mcp-gateway` and only listed human principals approve. Per-agent `requested_by` needs caller identity on the server (Milestone 1, item 6).
- The apply triggered by the approval channel does not pass through the gateway, so gateway policy (Cedar, once added) does not see it; the write guard and RBAC do.
- Slack is not an approval channel yet; the implemented channel is this IAM-authenticated API and its CLI.

## Agent guidance

Instructions for agents, derived from this contract; where they disagree, this contract is right.

- `AGENTS.md` (repository root): the tools, the plan, approval and apply flow, what to do on each error code, and the handling of untrusted text in tool output. Its first line is `<!-- kube-handrail:agents-md -->`. `scripts/check_contract.py` fails unless it names every code in the `Error codes` list above.
- `agent/skills/<name>/SKILL.md`: one directory per skill, holding only `SKILL.md`, with frontmatter of exactly a one-line `name` (the directory name, `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`, at most 64 characters) and a one-line `description` (at most 1024 characters), then the line `<!-- kube-handrail:skill -->` right after the frontmatter. Files and directories whose names start with `.` are ignored. Today: `kube-handrail-scale`.

`scripts/devops_agent_register.py install-guidance` installs them as DevOps Agent assets in an agent space, after validating every file locally:

- the `agents_md` asset for `--agent-type` (default `GENERIC`, every agent), with metadata `{"agent_type": ...}` and the single file `AGENTS.md` (at most 25 000 bytes). An agent space holds one per agent type; an existing one is updated only if its `AGENTS.md` starts with the marker line, or with `--replace-instructions`, so standing instructions written by an operator are never overwritten silently. `GENERIC` instructions are loaded by every agent type in addition to that type's own, so installing for `CHAT` next to a `GENERIC` install gives chat sessions the same text twice: install for one agent type or the other;
- one `skill` asset per skill, found by `metadata.name`, with metadata `name`, `description` and `agent_types: ["GENERIC"]` (skills load only when relevant, so every agent type gets them whatever `--agent-type` says) and the single file `SKILL.md`. Like `AGENTS.md`, an existing skill of the same name is updated only if its `SKILL.md` has the marker line after the frontmatter, or with `--replace-instructions`. An update merges metadata, so a skill's `status` set by an operator is kept; `agent_types` is set back to `GENERIC`.

Existing assets are found with `ListAssets` and checked before the first write; they are updated with `UpdateAsset`, and more than one match is an error. `status` lists the `agents_md` assets and the skills named in `agent/skills/`; `remove-guidance` deletes the `agents_md` assets and those skills whose files carry the marker. `status` reports `guidance.error` instead of the assets when the caller may not list them. Agents load the instructions when a session starts, so a session that was already open keeps the old text.

## Runtime and packaging

- Python 3.12, `x86_64`, pinned in one Terraform local.
- Dependencies installed by Terraform-driven build into `build/<function>/` with `pip install --target build/<function> --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12 -r <requirements>`, plus `mcp/common/`. `archive_file` zips `build/<function>/`. The tool Lambdas use `requirements.txt`; the approver uses `mcp/approver/requirements.txt`, which installs nothing (it needs only the runtime's `boto3` and `handrail_common`, never `kubernetes`); the sentinel uses `mcp/sentinel/requirements.txt`, which bundles its own `boto3` because the runtime's copy may not know the `devops-agent` service. That pin must match the `boto3` pin in `requirements-dev.txt`, whose service models include `devops-agent` and `bedrock-agentcore-control`.

## Identity

- Two IAM roles, one per tool Lambda. On the plan table: read role `dynamodb:PutItem`, `dynamodb:GetItem` and `dynamodb:UpdateItem` (for `request_approval` and its counter); write role `dynamodb:GetItem` and `dynamodb:UpdateItem`; both `dynamodb:Query` on the `object-history` index ARN only. The read role also has `sns:Publish` on the approval topic, and an explicit deny of `PutItem` and `UpdateItem` that name any approval attribute (`approved_*`, `approval_channel`, `approval_expires_at`, `rejected_*`, `reject_reason`, `decision_correlation_id`, through `dynamodb:Attributes`). The write role has no such deny, because its consume condition reads those attributes; nothing in IAM keeps it from writing them. What is guaranteed: the read role cannot write an approval, the approver refuses the Lambda and gateway roles, and the write guard re-validates the consumed record against the request's arguments before it touches the cluster.
- Approver role: `dynamodb:GetItem` and `dynamodb:UpdateItem` on the plan table, `lambda:InvokeFunction` on the write Lambda only, its own log group. No EKS access: the apply it triggers runs as the write Lambda.
- Sentinel role: `ssm:GetParameter` and `ssm:PutParameter` on the kill switch parameter only; `bedrock-agentcore:ListGatewayTargets` and `bedrock-agentcore:GetGatewayTarget` on this gateway; `aidevops:GetAgentSpace` and `aidevops:GetAssociation` on the configured agent space and association (only when configured); `sns:Publish` on the alert topic; its own log group. No EKS access.
- Two EKS access entries, type `STANDARD`, with `kubernetes_groups` `kube-handrail-reader` and `kube-handrail-writer`. No access policy associations.
- RBAC in the sandbox namespace only:
  - reader: `get`, `list` on `deployments` and `horizontalpodautoscalers`; `list` only on `poddisruptionbudgets` (`policy`), `services` and `endpointslices` (`discovery.k8s.io`), and, for `get_workload_events`, `replicasets` (`apps`) and `events` (core). No access to pods: the impact preview takes ready counts from the Deployment status and readiness from EndpointSlices, and events name pods without the reader reading them, so the reader never sees pod specs or environment variables;
  - writer: `get`, `patch` on `deployments`.

## Configuration (environment variables)

`CLUSTER_NAME`, `PLANS_TABLE`, `ALLOWED_NAMESPACES` (comma-separated), `MIN_REPLICAS`, `MAX_REPLICAS`, `PLAN_TTL_SECONDS`, `REMEDIATION_ENABLED_PARAM` (write only), `METRIC_NAMESPACE` (the [tool record](#observability)'s metric namespace; Terraform sets `<name_prefix>/tools`, default `kube-handrail/tools`). `AWS_REGION` is provided by Lambda.

Server-side approval, set on both tool Lambdas (the approver reads `APPROVAL_MODE` and `APPLY_AFTER_APPROVAL_TTL_SECONDS` as well):

| Variable | Default | Terraform variable |
|---|---|---|
| `APPROVAL_MODE` | `server` (also when unset) | `approval_mode` (`client`, `server`, `both`) |
| `APPROVAL_WAIT_TTL_SECONDS` | 900; at least `PLAN_TTL_SECONDS` | `approval_wait_ttl_seconds` |
| `APPLY_AFTER_APPROVAL_TTL_SECONDS` | 120 | `apply_after_approval_ttl_seconds` |
| `APPROVAL_REQUEST_COOLDOWN_SECONDS` | 60 (`0` turns it off) | `approval_request_cooldown_seconds` |
| `APPROVAL_REQUEST_LIMIT` | 10 per 600 s | `approval_request_limit` |

Read Lambda only: `APPROVAL_TOPIC_ARN` (the approval topic) and `APPROVAL_URL` (shown in notifications). Approver: `PLANS_TABLE`, `WRITE_FUNCTION_NAME`, `APPROVER_PRINCIPALS` and `DENIED_PRINCIPALS` (comma-separated IAM user or role ARNs). Terraform variables: `approver_principals` (required in `server` and `both` mode; must not list the Lambda or gateway roles), `approval_require_mfa` (default `true`), `approval_email`; outputs `approval_url`, `approval_topic_arn`, `approver_policy_arn`.

History checks, set on both Lambdas; each must be a positive integer, except that the cooldown may be 0:

| Variable | Default | Terraform variable |
|---|---|---|
| `FLAP_WINDOW_SECONDS` | 600 | `flap_window_seconds` |
| `OBJECT_HOURLY_APPLY_BUDGET` | 6 | `object_hourly_apply_budget` |
| `PLAN_RATE_LIMIT` | 20 | `plan_rate_limit` |
| `PLAN_RATE_WINDOW_SECONDS` | 600 | `plan_rate_window_seconds` |
| `APPLY_COOLDOWN_SECONDS` | 60 (`0` turns it off) | `apply_cooldown_seconds` |
| `GLOBAL_HOURLY_APPLY_BUDGET` | 30 | `global_hourly_apply_budget` |

Blast-radius budget, set on both Lambdas; a positive integer:

| Variable | Default | Terraform variable |
|---|---|---|
| `MAX_REPLICA_STEP_PERCENT` | 100 | `max_replica_step_percent` |
