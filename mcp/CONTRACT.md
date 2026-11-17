# Tool contract

Single source of truth for handlers, infrastructure code and tests. Change this file first. Every other part is defined in an appendix under `mcp/contract/`, listed in [Appendices](#appendices); an appendix is as binding as this file.

## Targets and tools

| Gateway target | Lambda | Classification | Tools (schema file) |
|---|---|---|---|
| `k8sr` | `k8s_read` | `READ_ONLY` | `get_workload_status`, `get_workload_events`, `get_rollout_history`, `plan_scale_deployment`, `plan_restart_deployment`, `plan_rollback_deployment`, `plan_adjust_hpa`, `request_approval`, `get_plan_outcome` (`mcp/k8s_read/tools.json`) |
| `k8sw` | `k8s_write` | `MUTATIVE` | `apply_scale_deployment`, `apply_restart_deployment`, `apply_rollback_deployment`, `apply_adjust_hpa` (`mcp/k8s_write/tools.json`) |

Each action has one `plan_<action>` tool and one `apply_<action>` tool; apply takes the plan's arguments plus `plan_id`.

| Tools | Arguments | Defined in |
|---|---|---|
| `plan_scale_deployment`, `apply_scale_deployment` | `namespace`, `name`, `replicas`, optional boolean `override_manual_change` (default `false`; any other type is `invalid_argument`) | [Guard checks](#guard-checks), [History checks](contract/history-checks.md) |
| `plan_restart_deployment`, `apply_restart_deployment` | `namespace`, `name` | [Restart](contract/restart-rollback.md#restart) |
| `plan_rollback_deployment`, `apply_rollback_deployment` | `namespace`, `name`, `revision` (an integer from 1 to 999999999999999999), optional `override_manual_change` | [Rollback](contract/restart-rollback.md#rollback) |
| `plan_adjust_hpa`, `apply_adjust_hpa` | `namespace`, `name` (a HorizontalPodAutoscaler), integers `min_replicas` and `max_replicas` (at least one), optional `override_manual_change` | [Adjust HPA](contract/adjust-hpa.md) |
| every `plan_*` and `apply_*` | optional integer `verify_window_seconds` and string [`change_ref`](contract/change-ref.md); scale and HPA adjust also optional boolean `revert_on_failure` | [Verification and revert](contract/verification.md) |
| `request_approval` | only `plan_id` | [Approval API](contract/approval-api.md#request_approvalplan_id) |
| `get_plan_outcome` | only `plan_id` | [Verification and revert](contract/verification.md#get_plan_outcomeplan_id) |
| `get_workload_status` | `namespace`, `name` | [Untrusted output](contract/untrusted-output.md) |
| `get_workload_events` | `namespace`, `name`, optional `limit` | [Workload events](contract/untrusted-output.md#workload-events) |
| `get_rollout_history` | `namespace`, `name` | [Rollout history](contract/restart-rollback.md#rollout-history) |

Clients see `<target>___<tool>`, for example `k8sw___apply_scale_deployment`. Full names must be at most 64 characters and match `^[a-z0-9_]+$`.

The classification of every tool is in `mcp/classification.json`, a map from full tool name to `READ_ONLY` or `MUTATIVE` (`DESTRUCTIVE` is never used). It is the only place a classification is defined: `scripts/devops_agent_register.py` registers from it (`associate-read` the `READ_ONLY` tools; `enable-write` sets the association to exactly the classified tools, which also removes any tool added by hand), Terraform passes it to the drift sentinel, and `scripts/check_contract.py` (run in CI) fails unless:

- every tool in `mcp/*/tools.json` has a classification, and every classified tool exists;
- every tool of the write target `k8sw` (`mcp/k8s_write/tools.json`), and every `apply_*` tool on any target, is `MUTATIVE`. The sentinel and the registration script refuse a classification that breaks this rule as well, so a tampered `TOOL_CLASSIFICATION` cannot make a downgraded write tool look expected;
- every full name is at most 64 characters and matches `^[a-z0-9_]+$`;
- every `plan_<action>` tool has an `apply_<action>` tool and every `apply_<action>` tool a `plan_<action>` tool;
- `terraform/main.tf` loads each `mcp/<function>/tools.json` under the target name used in the classification;
- no target is named `kh_internal`, and no `tools.json` names an internal handler (below).

## Dispatch

The tool name is the part after `___` in `context.client_context.custom["bedrockAgentCoreToolName"]`. The event is the tool arguments object. There is no `tool_name` argument and no fallback. An unknown tool returns error code `unknown_tool`.

The write Lambda also has internal handlers, which no MCP client can call: the full name `kh_internal___<name>` runs the internal handler `<name>` (today only `verify_applied_plan`, [Verification and revert](contract/verification.md)), and only when the invocation's [channel](contract/identity.md#agent-identity) is `verifier`. Every other combination is `unknown_tool`: the reserved target on another channel, a gateway target on the `verifier` channel, and an internal handler's name under a gateway target (`k8sw___verify_applied_plan`). The gateway always calls a tool as `<target>___<tool>` of a configured target, and a gateway client can set neither the tool name nor `client_context.custom`. `scripts/check_contract.py` refuses a target named `kh_internal` and an internal handler's name in any `tools.json`.

## Response envelope

Every handler returns a JSON object (not wrapped in `statusCode`/`body`):

```json
{ "ok": true, "data": {}, "error": null, "truncated": false }
{ "ok": false, "data": null, "error": { "code": "plan_expired", "message": "..." }, "truncated": false }
```

`error` may also contain an optional `details` object with structured context, omitted when there is nothing to add. Every value in the history checks' `details` is an integer or a string. Error envelopes carry no correlation ID, because `error` and its `details` are a fixed contract that clients and tests match exactly ([Observability](contract/observability.md)).

Error codes: `invalid_argument`, `unknown_tool`, `not_found`, `forbidden_namespace`, `out_of_bounds`, `gitops_managed`, `plan_not_found`, `plan_expired`, `plan_used`, `plan_mismatch`, `conflict`, `remediation_disabled`, `flapping`, `object_budget_exceeded`, `plan_rate_limited` (deprecated, never returned), `manual_change`, `cooldown`, `global_budget_exceeded`, `agent_budget_exceeded`, `blast_radius_exceeded`, `approval_required`, `approval_rate_limited`, `plan_decided`, `revision_not_found`, `forbidden`, `change_frozen`, `admission_denied`, `kubernetes_error`, `internal`.

### Error code table

| Code | Returned when | `details` |
|---|---|---|
| `invalid_argument` | An argument fails validation (also `revert_on_failure` without `verify_window_seconds` or on a restart or rollback, a rollback to the current template, HPA bounds moving in opposite directions); `request_approval` in `client` mode; a bad approval API request. | none |
| `unknown_tool` | The tool is not served, or an internal handler is called outside the `verifier` channel ([Dispatch](#dispatch)). | none |
| `not_found` | The Deployment (or HPA) does not exist. | none |
| `forbidden_namespace` | The namespace is not allowed, or its policy does not allow the action ([Namespace policy](#namespace-policy)). | none for a namespace not allowed at all; `{"action": "scale" \| "restart" \| "rollback" \| "adjust_hpa", "allowed_actions": [...]}` otherwise |
| `out_of_bounds` | `replicas`, an HPA bound, or the revert target of `revert_on_failure` is outside the namespace's bounds. | names the bound or revert target |
| `gitops_managed` | The object has a GitOps marker ([guard check 4](#guard-checks)). | none |
| `plan_not_found` | No plan with that `plan_id`. | none |
| `plan_expired` | `expires_at <= now`, or the record has no `args`. | none |
| `plan_used` | The plan was already consumed. | none |
| `plan_mismatch` | The apply's arguments, or the consumed record, differ from the plan. | none |
| `conflict` | The object changed since the plan; its rollout is paused; a pinned ReplicaSet was replaced or edited; an HPA owns the replica count; the admission policy does not back the bounds; a 409 persisted after all patch attempts; a record changed concurrently (`request_approval`, approval API); an HPA's spec changed or its target is scaled to 0 ([Adjust HPA](contract/adjust-hpa.md)). | `{"generation": {"planned", "current"}, "replicas": {"planned_from", "current"}}` (an unset `spec.replicas` counts as 1); `{"uid": {"planned", "current"}}` for a Deployment deleted and recreated; `{"paused": true}` (restart and rollback, plan and apply); `{"replica_set": {"planned", "current"}}` or `{"template": {"planned", "current"}}` (rollback); `{"hpa": [<names>], "hint": "use plan_adjust_hpa on <first name>"}`, with the hint also ending the message; `{"check": "admission_policy_match", "reason", "policy": "kube-handrail-writer-bounds", ...}` ([Plan-time check](contract/admission-network.md#plan-time-check)); none for a persistent 409 |
| `remediation_disabled` | The global kill switch is off, the agent's own kill switch is off, or the freezes cannot be read or do not validate. | none for the global switch; `{"scope": "agent", "agent": "<name>"}`; `{"scope": "freezes", "reason": "unreadable" \| "invalid"}` |
| `flapping` | A second reversal of direction within `FLAP_WINDOW_SECONDS`. | `{"window_seconds", "count", "requested": {"from", "to"}, "history"}` |
| `object_budget_exceeded` | `OBJECT_HOURLY_APPLY_BUDGET` applies to the object in the last 3600 s. | `{"window_seconds", "limit", "count", "history"}` |
| `plan_rate_limited` | Never returned (the plan rate limit was removed; listed for one release). | - |
| `manual_change` | The object was changed outside kube-handrail since its last apply. | `{"last_applied_to", "current"}`, or `{"last_applied_template", "current_template"}`; for an HPA each bound pair is `{min_replicas, max_replicas}` |
| `cooldown` | The last apply is less than `APPLY_COOLDOWN_SECONDS` old. | `{"cooldown_seconds", "last_applied_at", "retry_after", "history"}` |
| `global_budget_exceeded` | The hour's global apply budget is spent. | `{"limit", "window_start", "retry_after"}` |
| `agent_budget_exceeded` | The hour's apply budget of the plan's agent is spent. | `{"agent", "limit", "window_start", "retry_after"}` |
| `blast_radius_exceeded` | The step is larger than the blast-radius budget. | `{"from", "to", "max_step", "max_step_percent"}` |
| `approval_required` | No valid server approval ([Apply condition](#apply-condition)). | `{"status": "planned" \| "awaiting_approval" \| "rejected" \| "approval_expired" \| "approval_invalid"}` |
| `approval_rate_limited` | `APPROVAL_REQUEST_LIMIT` approval requests in the current 600-second window. | `{"limit", "window_seconds": 600, "window_start", "retry_after"}` |
| `plan_decided` | The plan was already approved or rejected. | `{"status": "approved" \| "rejected"}` |
| `revision_not_found` | The revision has no ReplicaSet of the Deployment. | plan: `{"revision", "available"}` (newest first, at most 20); apply: `{"revision", "replica_set"}` |
| `forbidden` | The caller is not a configured agent, or not the plan's requester; the approval API refuses the caller. | `{"reason": "agent_unknown" \| "not_requester"}`; approval API: none, or `{"reason": "not_namespace_approver", "namespace"}` |
| `change_frozen` | A change freeze covers the request ([Change freezes](contract/freezes.md)). | `{"namespace", "reason", "until"}` |
| `admission_denied` | The cluster's admission policy refused the apply's dry run ([Admission policy](contract/admission-network.md)). | `{"policy", "binding"}` |
| `kubernetes_error` | The Kubernetes API failed or refused a call in a way not listed above. | none |
| `internal` | Any other server error, including an unreadable history. | none |

`history` lists up to the 20 most recent applies in the check's window, oldest first, each `{applied_at, from, to}`; `count` is the number of applies in the window.

## Plan record (DynamoDB)

| Attribute | Type | Notes |
|---|---|---|
| `plan_id` | S | partition key, random UUID |
| `action` | S | `scale_deployment`, `restart_deployment`, `rollback_deployment` or `adjust_hpa` |
| `namespace`, `name` | S | target |
| `replicas_from`, `replicas_to` | N | for a restart or a rollback both are the current `spec.replicas` (unset counts as 1) |
| `args_hash` | S | SHA-256 of `json.dumps({"action": action, **args}, sort_keys=True, separators=(",", ":"))`. For a scale that is `{action, namespace, name, replicas}`, for a restart `{action, namespace, name}`, for a rollback `{action, namespace, name, revision}`; the scale and rollback objects gain `"override_manual_change": true` only when the override is true, every object gains `"verify_window_seconds": N` only when the window was given, and the scale object `"revert_on_failure": true` only when it is true, so a hash without them is the same as before they existed |
| `args` | M | the payload `args_hash` covers, without `action`: the apply arguments without `plan_id`, each optional argument only when `args_hash` includes it, and [`change_ref`](contract/change-ref.md) when given. The approval channel forwards it unchanged as the apply arguments, and apply compares the request's arguments with it. Records made before it have none; they can be neither approved nor applied and must be planned again |
| `template_from`, `template_to` | S | `sha256:` and the hex SHA-256 of `json.dumps(template, sort_keys=True, separators=(",", ":"))`, where `template` is the Deployment's `spec.template` as the API server returned it at plan time (raw JSON: client models drop unknown fields), and the template the change leaves. A scale does not change the template, so both are the current hash. A restart's `template_to` is the hash of the current template with the pod-template annotation `kube-handrail.io/restart-plan-id` set to the plan's own `plan_id`. A rollback's `template_to` is the hash of the target ReplicaSet's template ([Restart and rollback](contract/restart-rollback.md)). Recorded for every action; the template manual-change rule reads them back. Records made before them have neither |
| `revision_from` | N | the Deployment's `deployment.kubernetes.io/revision` annotation at plan time, only for an action that changes the template (restart, rollback) and only when the annotation is an integer. Information only |
| `revision_to`, `target_rs_name`, `target_rs_uid` | N, S, S | rollback only: the requested revision (the approved argument), and the name and `uid` of the ReplicaSet it resolved to, which apply re-reads |
| `images`, `fields_changed` | L, L | rollback only: `diff.images` and `diff.fields_changed` of the plan response, so the approval channel shows them from the record. Images are stored as untrusted text; field names only, never values |
| `diff_truncated` | BOOL | rollback only: whether `images` or `fields_changed` was cut, so the approval channel says the lists are incomplete |
| `resource_version` | S | Deployment `metadata.resourceVersion` at plan time (informational) |
| `generation` | N | Deployment `metadata.generation` at plan time; a record without it is treated as `-1` and never matches |
| `uid` | S | Deployment `metadata.uid` at plan time; a record without it is treated as `""` and never matches |
| `object_key` | S | `<namespace>/<name>/<uid>`; partition key of the `object-history` index |
| `created_at` | N | epoch seconds; sort key of the `object-history` index |
| `expires_at` | N | epoch seconds, end of plan validity: `created_at + PLAN_TTL_SECONDS`; `created_at + APPROVAL_WAIT_TTL_SECONDS` once an approval is requested; `approved_at + APPLY_AFTER_APPROVAL_TTL_SECONDS` once approved; `rejected_at` once rejected |
| `retain_until` | N | epoch seconds, the table TTL attribute: `created_at + max(3600, FLAP_WINDOW_SECONDS, APPLY_COOLDOWN_SECONDS) + max(PLAN_TTL_SECONDS, APPROVAL_WAIT_TTL_SECONDS) + APPLY_AFTER_APPROVAL_TTL_SECONDS + 60`, the longest window plus the longest a plan can stay valid, so a record stays readable as history after the plan expired, plus `VERIFY_WINDOW_MAX_SECONDS + VERIFY_GRACE_SECONDS`, so it stays until its verification has run. A record without it is kept until deleted |
| `used` | BOOL | apply sets it with one conditional update: `used = false AND expires_at > now AND args_hash = :h AND action = :a AND attribute_not_exists(rejected_by)`, extended per [Apply condition](#apply-condition) |
| `requested_by` | S | the requesting principal: `agent:<name>`, or the constant `mcp-gateway` (the class of the gateway's machine identities) on records made before agent identity, and `system:revert` on a revert record. The approver refuses to decide on a record whose requester starts with `system:` |
| `agent`, `requested_client_id` | S, S | the requesting agent's name and the verified OAuth `client_id` it planned with, kept as evidence because the name-to-client mapping can change; a revert record copies them from the plan it reverts |
| `channel` | S | the channel the apply ran under, `gateway` or `approval-api`, written by the conditional update that consumes the plan; absent until then. `verifier` on a revert record |
| `verify_window_seconds` | N | the verification window, the `verify_window_seconds` argument or, when it was not given, `VERIFY_WINDOW_DEFAULT_SECONDS`. Whether it was given is visible in `args` |
| `verify_at`, `verify_queue` | N, S | written by the conditional update that consumes the plan: `verify_at = now + verify_window_seconds` (`VERIFY_WINDOW_DEFAULT_SECONDS` for a record without it) and `verify_queue = "due"`, the partition key of the `verify-due` index. `verify_queue` is removed once the verification is final |
| `revert_to` | N | only for an apply with `revert_on_failure`: `replicas_from`, copied by the conditional update that consumes the plan, from the same record the apply then checks against the Deployment. The count a revert restores; a later change to `replicas_from` does not move it |
| `applied_generation` | N | written with `result` after a successful patch: the Deployment's `metadata.generation` from the patch response. Evidence only |
| `verification` | M | written once by the verifier channel: `{outcome, reason, checked_at, observed, correlation_id}` |
| `revert` | M | written once by the verifier channel, only for a plan with `revert_on_failure` whose verification is `degraded`: `{status, reason, revert_plan_id, at}` ([Revert](contract/verification.md#revert)) |
| `target_kind`, `hpa_target`, `min_from`, `min_to`, `max_from`, `max_to`, `spec_from`, `spec_to`, `revert_min_to`, `revert_max_to` | S, S, N, N, N, N, S, S, N, N | HPA adjust only (`target_kind` `HorizontalPodAutoscaler`; absent means Deployment): the HPA's scale target, its bounds before and after, the hash of its spec before and after, and the bounds a revert restores, pinned by the consume like `revert_to` ([Adjust HPA](contract/adjust-hpa.md)) |
| `kind`, `reverts_plan_id` | S, S | revert records only: `revert`, and the `plan_id` of the plan whose apply the record undoes |
| `cluster` | S | the cluster the plan is for: the read Lambda's `CLUSTER_NAME`; a revert record copies it from its plan. The approval channel shows it. Records made before it have none |
| `status` | S | `planned`, `awaiting_approval`, `approved` or `rejected`, and `revert` on a revert record, which can never be requested, approved or applied. `applied` (`used = true`) and `expired` (`expires_at <= now`) are derived, never stored. Records made before server approval have no `status` and `requested_by`; they can be applied in `client` mode until they expire but never requested or approved |
| `override_manual_change` | BOOL | the value pinned in `args_hash` |
| `impact` | M | the impact preview returned with the plan, so the approval channel renders the server's own record |
| `approval_requested_at` | N | last `request_approval` that notified |
| `approval_mode` | S | the read Lambda's `APPROVAL_MODE` at plan time (a revert record copies its plan's). Evidence only: apply checks its own |
| `approved_by`, `approved_at`, `approved_args_hash`, `approval_channel`, `approval_expires_at` | S, N, S, S, N | written by an approval: the approver's IAM principal ARN as it signed the request, the time, the plan's own `args_hash` copied in the same update, `api`, and `approved_at + APPLY_AFTER_APPROVAL_TTL_SECONDS` |
| `rejected_by`, `rejected_at`, `reject_reason` | S, N, S | written by a rejection; the reason is at most 500 characters with control characters removed |
| `applied_at`, `result`, `apply_correlation_id` | N, S, S | written by apply after the patch, on a best-effort basis: `result` is `applied`, or the error code that stopped the apply; `apply_correlation_id` is the apply's correlation ID. An applied plan can therefore lack them; the Deployment's `kube-handrail.io/last-plan-id` annotation is the authoritative record of the last apply |
| `plan_correlation_id`, `approval_request_correlation_id`, `decision_correlation_id` | S | the correlation IDs of the plan, of the last `request_approval` that notified, and of the approval or rejection ([Observability](contract/observability.md)). Each is absent until its step happens; records made before them have none |

Global secondary index `object-history`: partition key `object_key` (S), sort key `created_at` (N), projection `ALL`. It is eventually consistent, so a plan or apply from the last moment may not be visible yet.

Global secondary index `verify-due`: partition key `verify_queue` (S), sort key `verify_at` (N), projection `KEYS_ONLY`. It is sparse: only records with `verify_queue` are in it, the plans whose verification is scheduled and not final.

The table also holds counters, which have no `object_key` and so are not in the index:

- one global budget counter per clock hour: `plan_id = "budget#global#<hour start, epoch seconds>"` (never a UUID, so it cannot be named as a plan), `applies` (N), `retain_until` two hours after the hour starts;
- one apply counter per agent and clock hour: `plan_id = "budget#agent#<name>#<hour start>"` (`#` cannot occur in an agent name), `applies` (N), `retain_until` as for the global counter;
- one approval request counter per 600-second clock window: `plan_id = "approval#global#<window start>"`, `requests` (N), `retain_until` two windows after the window starts.

## Guard checks

The steps of a scale. Restart, rollback ([restart-rollback.md](contract/restart-rollback.md)) and HPA adjust ([adjust-hpa.md](contract/adjust-hpa.md)) are defined as differences from them, by these numbers.

Plan (`k8s_read`):

1. Arguments valid: namespace and name match DNS-1123; `replicas` integer. The optional `verify_window_seconds`, `revert_on_failure` and `change_ref` are checked here too.
2. Namespace in `ALLOWED_NAMESPACES`, and the namespace's policy allows the action; otherwise `forbidden_namespace`.
3. The namespace's bounds: `min_replicas <= replicas <= max_replicas` of its policy (`MIN_REPLICAS` and `MAX_REPLICAS` without a policy file); otherwise `out_of_bounds`.
   - Then the caller is resolved to its agent (`forbidden`, `agent_unknown`; [Agent identity](contract/identity.md)), and the agent's kill switch must be on (`remediation_disabled`, `details.scope = "agent"`).
   - Then the [change freezes](contract/freezes.md) (`change_frozen`). Nothing is read from the cluster before this point.
   - 3a. Admission policy: the cluster's ValidatingAdmissionPolicy and binding back the namespace's bounds in `Deny`; otherwise `conflict` with `details.check = "admission_policy_match"` ([Plan-time check](contract/admission-network.md#plan-time-check)). Read before the Deployment. Skipped only with `ADMISSION_CHECK=false`.
4. Deployment exists and is not GitOps-managed: it has none of the Argo CD annotation `argocd.argoproj.io/tracking-id` (annotation tracking), the Flux labels `kustomize.toolkit.fluxcd.io/name`, `kustomize.toolkit.fluxcd.io/namespace`, `helm.toolkit.fluxcd.io/name` and `helm.toolkit.fluxcd.io/namespace`, and no label or annotation named in `GITOPS_MARKERS` or the namespace's `gitops_markers`. Malformed labels or annotations count as managed. `app.kubernetes.io/instance` alone is not a marker, because Helm sets it on every release; a deployment whose Argo CD uses label tracking, which writes only that label, lists it in `GITOPS_MARKERS` (Terraform `gitops_markers`).
5. Deployment has no HorizontalPodAutoscaler targeting it; otherwise `conflict` with `details.hpa` and the hint.
6. Blast-radius budget: `|replicas - current| <= max(1, ceil(current * MAX_REPLICA_STEP_PERCENT / 100))`, where `current` is `spec.replicas` (unset counts as 1); otherwise `blast_radius_exceeded`. It is enforced whatever the approval says. Then, with `revert_on_failure`, the revert target must be within the bounds ([Verification and revert](contract/verification.md#arguments)).
7. History checks: manual change, cooldown, flapping, object budget ([History checks](contract/history-checks.md)).
8. Global budget: the current hour's counter, read consistently, must be below `GLOBAL_HOURLY_APPLY_BUDGET`. Then the agent's counter for the hour, read consistently, must be below its budget (`agent_budget_exceeded`). Planning only reads both.
9. Impact preview: list the PodDisruptionBudgets, Services and EndpointSlices in the namespace ([Impact preview](contract/impact.md)). Any error fails the plan with `kubernetes_error`, so the approver never sees a preview with gaps. Only then is the plan record written.

The plan response `data` has `plan_id`, `diff`, `override_manual_change` (the value pinned in `args_hash`), `preflight`, `impact`, `expires_at`, `requested_by`, `correlation_id` and `verification`. `preflight` lists the checks that passed, in this order: `arguments`, `agent_enabled`, `namespace_allowed`, `action_allowed`, `replica_bounds`, `admission_policy_match` (`{"passed": true, "skipped": true}` with `ADMISSION_CHECK=false`), `not_gitops_managed`, `no_hpa`, `blast_radius`, `revert_target_in_bounds` (only with `revert_on_failure`), `no_manual_change`, `cooldown`, `not_flapping`, `object_budget`, `plan_rate`, `global_budget`, `agent_budget`. `plan_rate` is always `{"passed": true, "skipped": true}`: the plan rate limit was removed, and the entry stays for one release so clients that read it keep working.

Apply (`k8s_write`), all re-checked independently of the plan step:

1. Kill switch: SSM parameter `REMEDIATION_ENABLED_PARAM` must equal `true`, read on every apply without caching, so turning it off takes effect immediately. A gateway apply then resolves its caller to an agent (`forbidden`, `agent_unknown`).
2. Checks 1-3 above, with the namespace policy in force at apply time, so a namespace or action withdrawn since the plan is refused (`forbidden_namespace`) and the plan is not consumed. Check 3a is not re-run (the cluster's backstop refuses an out-of-bounds patch itself); check 4 is re-run in step 4, check 6 in step 3; check 5 is not, because the writer identity cannot list HPAs; the impact preview is not, because it only informs the approver. Then the [change freezes](contract/freezes.md), with the time of the apply; a refusal leaves the plan unconsumed.
3. Plan exists, `action` matches, `args_hash` matches, not expired, not used, and in `server` and `both` mode approved; a gateway apply also requires the plan's `requested_by` to be the caller's agent ([Apply condition](#apply-condition)). Mark used with that one conditional update before patching; the same update records `channel` and schedules the verification ([Scheduling](contract/verification.md#scheduling)). Right after it, on every channel, a plan without `agent` is refused (`forbidden`, `agent_unknown`; only the approval channel can get that far with one), and the kill switch of the plan's agent must be on (`remediation_disabled`, `details.scope = "agent"`). Then the blast-radius budget (plan check 6), with the plan's `replicas_from` and `replicas_to` and the current `MAX_REPLICA_STEP_PERCENT`, so lowering the setting also stops plans made before the change. Each refusal in this step after the consume leaves the plan consumed with its code as `result`.
4. Read the Deployment. Its `metadata.uid` must equal the planned `uid` (a Deployment deleted and recreated under the same name starts again at generation 1), it must not be GitOps-managed (check 4, re-run because a label-only change does not bump `generation`), and its `metadata.generation` must equal the planned `generation` and its `spec.replicas` (unset counts as 1) must equal `replicas_from`; otherwise `conflict` with `details`, or `gitops_managed`. The API server bumps a Deployment's `generation` on spec and annotation changes but not on status or label updates, so a rollout in progress does not block the apply. Patch `spec.replicas` with a `metadata.resourceVersion` precondition taken from that read. On 409, read again, re-run these checks and patch again, up to 3 attempts in total; a change found on a retry returns the error immediately, and a 409 on the last attempt returns `conflict`.
5. Server-side dry run (`dryRun=All`) before each real patch. A plan is consumed before the dry run, so any failure after step 3 requires a new plan.
6. The patch also sets the annotations `kube-handrail.io/last-plan-id` to the applied `plan_id`, `kube-handrail.io/correlation-id` to the apply's [correlation ID](contract/observability.md#correlation-id) and, for a plan that names its agent, `kube-handrail.io/requested-by` to `requested_by`.
7. History checks, after step 3 and after the first read in step 4 has passed: manual change, cooldown, flapping and object budget. They use the planned `replicas_from` and `replicas_to`; step 4 has confirmed that `replicas_from` is the current count. Then one conditional update adds 1 to the plan's agent's counter for the current hour, only while it is below the agent's budget (`agent_budget_exceeded`), whichever channel applies the plan; then one adds 1 to the global counter only while it is below `GLOBAL_HOURLY_APPLY_BUDGET`, so concurrent applies cannot overshoot it. Units are taken before the patch and not returned if the patch then fails. A 409 retry re-runs step 4 but not the history checks. A rejection here leaves the plan consumed, with the error code as its `result`.

A successful scale apply's `data` is `plan_id`, `namespace`, `name`, `replicas_from`, `replicas_to`, `resource_version`, `approved: true | false` and `correlation_id`; the approver's identity stays in the plan record and the logs.

## Limits

Every number a guard enforces. Settings are environment variables of both tool Lambdas, set from the Terraform variable of the same name in lower case, unless noted; each is a positive integer unless `0` is allowed.

| Kind | Limit | Setting (default) | Refusal |
|---|---|---|---|
| Size | Replica count, and an HPA's bounds, within the namespace's bounds | namespace `min_replicas`, `max_replicas` (0, 5); `MIN_REPLICAS`, `MAX_REPLICAS` without a policy file | `out_of_bounds`; in the cluster `admission_denied` |
| Size | Step per change: `max(1, ceil(current * percent / 100))` replicas (HPA: the pods its new bounds move) | `MAX_REPLICA_STEP_PERCENT` (100) | `blast_radius_exceeded` |
| Size | Tool output: a field at most 1024 characters, a list response at most 32768 bytes of `data`, at most 20 entries per preview list, revision images or rollback image list, 50 changed fields | fixed ([Untrusted output](contract/untrusted-output.md)) | `truncated: true` |
| Pattern | No second reversal of direction within the window | `FLAP_WINDOW_SECONDS` (600) | `flapping` |
| Pattern | No change over a manual change without `override_manual_change` | - | `manual_change` |
| Pace | Time between two applies to one object | `APPLY_COOLDOWN_SECONDS` (60; `0` turns it off) | `cooldown` |
| Pace | Plan validity | `PLAN_TTL_SECONDS` (1 to 3600; 300 when unset; Terraform picks 900 in `client` mode and 300 otherwise unless `plan_ttl_seconds` is set; output `plan_ttl_seconds`) | `plan_expired` |
| Pace | Validity once an approval is requested, from creation | `APPROVAL_WAIT_TTL_SECONDS` (900; at least `PLAN_TTL_SECONDS`) | `plan_expired` |
| Pace | Validity of an approval | `APPLY_AFTER_APPROVAL_TTL_SECONDS` (120) | `approval_required` (`approval_expired`) |
| Pace | Verification window, and how late a verdict or revert may come | `VERIFY_WINDOW_MIN_SECONDS` (60) to `VERIFY_WINDOW_MAX_SECONDS` (1800), default `VERIFY_WINDOW_DEFAULT_SECONDS` (120); `VERIFY_GRACE_SECONDS` (300). `60 <= MIN <= DEFAULT <= MAX` | `invalid_argument`; outcome `unknown` (`late`) |
| Volume | Applies to one object in the last 3600 s | `OBJECT_HOURLY_APPLY_BUDGET` (6) | `object_budget_exceeded` |
| Volume | Applies by one agent per clock hour | `AGENTS` `hourly_apply_budget`, else `AGENT_HOURLY_APPLY_BUDGET` (10) | `agent_budget_exceeded` |
| Volume | Applies across all objects per clock hour | `GLOBAL_HOURLY_APPLY_BUDGET` (30) | `global_budget_exceeded` |
| Volume | Plans verified per verifier run | `VERIFY_BATCH_LIMIT` (10; verifier, fixed by Terraform) | waits for the next run |
| Human attention | Approval notifications per 600-second clock window | `APPROVAL_REQUEST_LIMIT` (10) | `approval_rate_limited` |
| Human attention | Time before one plan notifies again | `APPROVAL_REQUEST_COOLDOWN_SECONDS` (60; `0` turns it off) | `notified: false` with `retry_after` |
| Human attention | A human approval of the exact arguments, in `server` and `both` mode | `APPROVAL_MODE` | `approval_required` |
| Human attention | Operator stops: global and per-agent kill switches, change freezes | SSM parameters | `remediation_disabled`, `change_frozen` |

Clock-hour and clock-window counters let up to twice a limit pass across a boundary. There is no plan rate limit: `PLAN_RATE_LIMIT` and `PLAN_RATE_WINDOW_SECONDS` are ignored.

## Server-side approval

Implements [ADR 0002](../docs/adr/0002-server-verified-approval.md). A human approval is stored in the plan record and checked by the apply's own conditional write. `request_approval`, the approval API and who may reach it are in [Approval API](contract/approval-api.md).

### Modes

`APPROVAL_MODE` (Terraform `approval_mode`, default `server`; an unset variable also means `server`, so a missing setting fails closed):

| Mode | Apply requires a server approval | Use |
|---|---|---|
| `client` | No; the condition is the one from Milestone 0. | DevOps Agent chat: the chat approval is the only one. |
| `server` | Yes. | Every other client, and DevOps Agent autonomous investigations (plan and `request_approval`, then a human approves through the approval channel, which also runs the apply). |
| `both` | Yes. The server cannot see the client's approval, so the apply condition is the one of `server`. The approval channel never runs the apply in this mode: the client applies, after its own approval. | DevOps Agent chat with a server approval too: the agent plans and calls `request_approval`, a human approves through the approval channel, then the agent applies in chat and the chat approval is asked as usual. |

Only in `server` mode does the approval channel run the apply. `request_approval` is refused with `invalid_argument` in `client` mode, where the client approves and a server approval would change nothing; the message is "client mode: apply directly, your client asks the human; approval is handled by the client in client mode" (the second part is the earlier message, for guidance of earlier releases). The approval API works in every mode; in `client` mode an approval is recorded but not required.

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

In every mode the consume condition refuses a rejected plan (`attribute_not_exists(rejected_by)`), even if the rejecting and the applying clock disagree about `expires_at`. A gateway apply adds `AND requested_by = :rb`, the caller's `agent:<name>` ([Agent identity](contract/identity.md)). In `server` and `both` mode it becomes

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
6. gateway apply, `requested_by` is not the caller's: `forbidden`, `details: {"reason": "not_requester"}`; the plan stays usable. A plan made before agent identity (`requested_by = "mcp-gateway"`) is therefore refused to every agent and must be planned again;
7. not approved (server and both): `approval_required`, `details: {"status": "planned" | "awaiting_approval"}`;
8. approved by the requester, or an approval of other arguments (only possible by writing the table directly): `approval_required`, `details: {"status": "approval_invalid"}`.

After the consume, the record's `args` must equal the request's arguments without `plan_id` (an absent or `false` override counts as not given), and its `namespace`, `name`, `replicas_to` and `override_manual_change` must match them (restart, rollback and HPA adjust add the record checks of their appendices) (`plan_mismatch` otherwise; a record without `args` is `plan_expired`, as at the approver), so a record altered in the table cannot redirect an apply even with a matching `args_hash`. Everything after the consume (blast radius, live object checks, history checks, budgets, dry run) is unchanged: an approval never lifts a guard.

## Namespace policy

Terraform `namespaces` maps each namespace the tools may act in to its policy:

| Field | Default | Meaning |
|---|---|---|
| `min_replicas`, `max_replicas` | 0, 5 | the replica bounds of [guard check 3](#guard-checks) and of the [admission policy](contract/admission-network.md) |
| `actions` | `restart`, `rollback`, `scale` | the actions plans and applies may take there; `adjust_hpa` only when listed; an empty set leaves the namespace read-only |
| `approvers` | none | IAM user or role ARNs that decide the namespace's plans; when set, only they, not `approver_principals` ([Approval API](contract/approval-api.md)); unused in `client` mode, where Terraform warns |
| `gitops_markers` | none | label or annotation keys that mark GitOps ownership there, in addition to `GITOPS_MARKERS` ([guard check 4](#guard-checks)) |

An empty map (the default) derives one entry from the deprecated `sandbox_namespace`, `min_replicas` and `max_replicas` (defaults `kube-handrail-sandbox`, 0, 5) with every action and no approvers of its own. Terraform refuses `namespaces` together with any of the three, and warns (check `deprecated_namespace_variables`) while any is set.

- **Where it lives.** Terraform renders `namespace_policy.json` (`{"namespaces": {"<ns>": {"min_replicas": N, "max_replicas": M, "actions": [...], "approvers": [...], "gitops_markers": [...]}}}`) into the packages of the tool Lambdas and the approver; it is in their source hashes, so a change redeploys them. Not an environment variable (the 4 KB limit) and not an SSM parameter: it widens access, so only a reviewed deploy changes it.
- **Loading.** An invalid file (an unknown action, bounds out of order, a bad namespace name or marker) fails the Lambda's start. `ALLOWED_NAMESPACES` must list exactly the file's namespaces, so a package and an environment from different applies cannot run together (cold starts can briefly fail during such an apply). `NAMESPACE_POLICY_FILE` names another path (tests); it must then exist. A package without the file falls back to `ALLOWED_NAMESPACES`, `MIN_REPLICAS` and `MAX_REPLICAS` with every action.
- **Checks.** An action the namespace does not allow is refused before the replica bounds, on plan and apply. The read tools work in every allowed namespace whatever its actions.
- **Reverts.** A pre-approved revert into a namespace that no longer allows the plan's action (`scale` or `adjust_hpa`), or is no longer allowed, is refused (`revert = {status: refused, reason: forbidden_namespace}`).
- **Global guards stay global.** The blast-radius and other budgets and the breaker apply alike in every namespace; there is no per-namespace budget. Change freezes already name namespaces.

## Agent guidance

Instructions for agents, derived from this contract; where they disagree, this contract is right.

- `AGENTS.md` (repository root): the tools, the plan, approval and apply flow, what to do on each error code, and the handling of untrusted text in tool output. Its first line is `<!-- kube-handrail:agents-md -->`. `scripts/check_contract.py` fails unless it names every code in the `Error codes` list above except the deprecated ones.
- `agent/skills/<name>/SKILL.md`: one directory per skill, holding only `SKILL.md`, with frontmatter of exactly a one-line `name` (the directory name, `^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`, at most 64 characters) and a one-line `description` (at most 1024 characters), then the line `<!-- kube-handrail:skill -->` right after the frontmatter. Files and directories whose names start with `.` are ignored. Today: `kube-handrail-scale` and `kube-handrail-rollout` (restart and rollback).

`scripts/devops_agent_register.py install-guidance` installs them as DevOps Agent assets ([guidance.md](contract/guidance.md)).

## Configuration (environment variables)

Limits are in [Limits](#limits). Others:

- **Tool Lambdas:** `CLUSTER_NAME`, `PLANS_TABLE`, `ALLOWED_NAMESPACES` (comma-separated; Terraform sets the namespaces of the [namespace policy](#namespace-policy)), `MIN_REPLICAS`, `MAX_REPLICAS` (the deprecated `min_replicas` and `max_replicas`, or 0 and 5; used only without a policy file), `NAMESPACE_POLICY_FILE` (optional; tests), `GITOPS_MARKERS` (a JSON array of label or annotation keys; unset or `[]` is none, anything that is not an array of valid keys fails the Lambda's start; Terraform `gitops_markers`, default `[]`), `REMEDIATION_ENABLED_PARAM` (write only), `METRIC_NAMESPACE` (Terraform sets `<name_prefix>/tools`, default `kube-handrail/tools`), `FREEZES_PARAM` (`/<name_prefix>/freezes`; unset means no freezes are checked; output `freezes_parameter`), and the identity variables below. `AWS_REGION` is provided by Lambda.
- **Read Lambda only:** `APPROVAL_TOPIC_ARN`, `APPROVAL_URL` (shown in notifications) and `ADMISSION_CHECK` (`true` or `false`; unset is `true`, anything else fails the start; Terraform `admission_check`, default `true`).
- **Approver:** `PLANS_TABLE`, `WRITE_FUNCTION_NAME`, `APPROVAL_MODE`, `APPLY_AFTER_APPROVAL_TTL_SECONDS`, `APPROVER_PRINCIPALS` and `DENIED_PRINCIPALS` (comma-separated IAM user or role ARNs), and the namespaces' own approvers from its packaged `namespace_policy.json`.
- **Verifier:** `PLANS_TABLE`, `WRITE_FUNCTION_NAME`, `VERIFY_BATCH_LIMIT`, `METRIC_NAMESPACE` (`<name_prefix>/verifier`). Output `verifier_function_name`. Terraform `verify_enabled` is deprecated and ignored: every apply is verified.
- **Audit Lambda:** `AUDIT_BUCKET`, `METRIC_NAMESPACE` (`<name_prefix>/audit`). Terraform `audit_enabled` (default `true`), `audit_object_lock_mode` (`GOVERNANCE`, default, or `COMPLIANCE`), `audit_retention_days` (30), `audit_force_destroy` (default `false`). Outputs `audit_bucket_name`, `audit_dlq_url`.
- **Sentinel:** in [Drift sentinel](contract/sentinel.md).

Agent identity, on both tool Lambdas ([Agent identity](contract/identity.md)):

| Variable | Notes | Terraform variable |
|---|---|---|
| `CALLER_KEY` | The key the interceptor signs the caller with, also set on the interceptor. Without it no caller is trusted, and every write path is refused with `agent_unknown` | generated once (`terraform_data.caller_key`) |
| `AGENTS` | JSON `{"<client_id>": {"name": "<agent>", "hourly_apply_budget": N}}`. Required, with at least one agent (the Lambda does not start without it); names match `^[a-z][a-z0-9_]{0,31}$` and are unique; the budget, when given, is a positive integer. | `agents`: a map from agent name to `{client_id = optional(string), hourly_apply_budget = optional(number)}`, default `{devops_agent = {}, smoke_test = {hourly_apply_budget = 30}}`; an entry without `client_id` gets a Cognito app client |
| `AGENT_HOURLY_APPLY_BUDGET` | 10; the budget of an agent that `AGENTS` gives none, and of a plan's agent no longer in `AGENTS` | `agent_hourly_apply_budget` |
| `AGENT_KILL_SWITCH_PREFIX` | `/<name_prefix>/agents/`; required | set from `name_prefix` |

Outputs: `agent_client_ids`, `cognito_client_ids`, `agent_kill_switch_parameters`.

Terraform variables for approval: `approval_mode` (`client`, `server`, `both`), `approver_principals` (in `server` and `both` mode every namespace that allows an action needs an approver, its own or these; neither may list the Lambda or gateway roles), `approval_require_mfa` (default `true`), `approval_email`; outputs `approval_url`, `approval_topic_arn`, `approver_policy_arn`. For namespaces and cluster manifests: `namespaces`, `admission_mode` (`Deny`, default, or `Warn`), `admission_check` (default `true`), and the deprecated `sandbox_namespace`, `min_replicas`, `max_replicas` (default `null`); outputs `namespaces`, `k8s_manifests`, `namespace_label_command`.

**Runtime and packaging.** Python 3.12, `x86_64`, pinned in one Terraform local. Terraform builds each function with `pip install --target build/<function> --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12 -r <requirements>` plus `mcp/common/`, and zips it. The tool Lambdas use `requirements.txt`; the approver's and verifier's install nothing (only the runtime's `boto3` and `handrail_common`, never `kubernetes`); the sentinel's bundles a `boto3` that knows the `devops-agent` service, pinned like `requirements-dev.txt`.

## Appendices

Each heading below is where a section used to be, so existing links resolve. [change-ref.md](contract/change-ref.md) defines `change_ref` and [guidance.md](contract/guidance.md) the guidance assets.

### Agent identity
The caller interceptor, agents, per-agent budget and kill switch, channels: [identity.md](contract/identity.md#agent-identity).
### Identity
IAM roles, EKS access entries and RBAC: [identity.md](contract/identity.md#iam-roles-and-rbac).
### Observability
Correlation IDs, the tool record, metrics and alarms: [observability.md](contract/observability.md).
### Untrusted output
Cleaning and wrapping of cluster text, response limits: [untrusted-output.md](contract/untrusted-output.md).
### Workload events
`get_workload_events`: [untrusted-output.md](contract/untrusted-output.md#workload-events).
### Rollout history
`get_rollout_history`: [restart-rollback.md](contract/restart-rollback.md#rollout-history).
### Restart
[restart-rollback.md](contract/restart-rollback.md#restart).
### Rollback
[restart-rollback.md](contract/restart-rollback.md#rollback).
### Adjust HPA
[adjust-hpa.md](contract/adjust-hpa.md).
### Impact preview
The preview and its warnings: [impact.md](contract/impact.md).
### History checks (oscillation breaker)
Manual change, cooldown, flapping, object budget: [history-checks.md](contract/history-checks.md).
### Verification and revert
Scheduling, the verifier, outcomes, the pre-approved revert, `get_plan_outcome`: [verification.md](contract/verification.md), the revert in [verification.md](contract/verification.md#revert).
### Change freezes
[freezes.md](contract/freezes.md).
### Approval API
`request_approval`, the approval API, MFA: [approval-api.md](contract/approval-api.md).
### Drift sentinel
[sentinel.md](contract/sentinel.md).
### Audit record
The write-once audit record and `scripts/audit.py`: [audit.md](contract/audit.md).
### Admission policy
The cluster admission policy and the plan-time check: [admission-network.md](contract/admission-network.md#admission-policy).
### Network
[admission-network.md](contract/admission-network.md#network).
