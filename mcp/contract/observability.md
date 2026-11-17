# Observability

Appendix to [the contract](../CONTRACT.md).

## Correlation ID

Dispatch takes each invocation's correlation ID from `context.client_context.custom`, using the first of these that is a string (or integer) matching `^[A-Za-z0-9._:-]{1,128}$`:

1. `bedrockAgentCoreAwsRequestId`, the gateway's ID for the request;
2. `kubeHandrailCorrelationId`, set only by the approver when it runs an apply (see [Approval API](approval-api.md#approval-api)), and by the verifier, to its own request ID, for the verifications it runs;

and otherwise the Lambda's own `aws_request_id`. A value that does not match is skipped, never logged. The gateway's `bedrockAgentCoreMcpMessageId` is not used: the client chooses the JSON-RPC message ID, so it is not unique across sessions. The approver is not behind the gateway; its correlation ID is its own Lambda request ID.

The ID is carried to:

- the tool record below, for every tool invocation;
- the plan record: `plan_correlation_id` (plan), `approval_request_correlation_id` (the `request_approval` call that notified), `decision_correlation_id` (the approver's request that approved or rejected), `apply_correlation_id` (the apply that recorded its `result`, whether it succeeded or not) and `verification.correlation_id` (the verifier run that checked it). See [Plan record](../CONTRACT.md#plan-record-dynamodb);
- the Deployment: the apply's patch sets the annotation `kube-handrail.io/correlation-id` to the apply's ID, next to `kube-handrail.io/last-plan-id`. Both are set by the same patch, which bumps `generation` once, as before;
- `data.correlation_id` in successful `plan_*`, `request_approval` and `apply_*` responses (`null` only outside a Lambda).
- the Kubernetes API: the User-Agent `kube-handrail/<version> plan/<plan_id> corr/<correlation_id>` (each part when known; `<version>` is `handrail_common.__version__`), for matching EKS audit log entries. The plan is the one being made (a refused plan names an ID never stored), applied, verified or reverted. Tested on a test cluster.

In `server` mode the approver passes its own request ID to the write Lambda as `kubeHandrailCorrelationId`, so the decision, the apply it runs, `apply_correlation_id` and the annotation share one ID, also when the apply fails.

The envelope itself is unchanged: error envelopes carry no correlation ID, because `error` and its `details` are a fixed contract that clients and tests match exactly. To find a refused call, search the tool records by `plan_id`, tool and time.

## Tool record

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
  "tool": "apply_scale_deployment", "correlation_id": "...", "caller": "<client_id>",
  "plan_id": "...", "outcome": "conflict", "reason": null, "scope": null, "duration_ms": 412
}
```

- `tool`: the tool name, `null` for an unknown tool (whose `Tool` dimension is `unknown`). An internal handler is logged under its name (`verify_applied_plan`).
- `caller`: the verified caller's `client_id` ([Agent identity](identity.md#agent-identity)), otherwise `null`.
- `plan_id`: the `plan_id` of a successful response's `data`, otherwise the `plan_id` argument, and only if it is a UUID; otherwise `null`.
- `outcome`: `ok` or the error code.
- `reason` and `scope`: a rejection's `details.reason` (`forbidden`) and `details.scope` (`remediation_disabled`), otherwise `null`. They are fixed words set by the server, so the metrics keep `Code` as their only rejection dimension. A `remediation_disabled` refused for unreadable freezes logs `reason` `unreadable` or `invalid`; the operator's free-text `reason` of `change_frozen` is never logged.
- `ToolCalls` is 1. `ToolErrors` is 1 for `internal` and `kubernetes_error` (the tool could not do its job, for example the Kubernetes API refused its credentials or returned a server error), and for `admission_denied` ([Admission policy](admission-network.md#admission-policy)). Any other error code is a guard rejection: the record then has `Code` and `GuardRejections = 1` and the second metric entry; an `ok`, `internal`, `kubernetes_error` or `admission_denied` record has neither. The empty dimension set gives `ToolCalls` and `ToolErrors` across all tools.

The record has only these fields. Argument values, tokens and error messages are never in it. An internal error additionally logs its stack trace through the Lambda logger, in a line that names the correlation ID.

Terraform creates these alarms on the drift sentinel's alert topic; they exist only with `sentinel_enabled`, which creates the topic:

- `<name_prefix>-tool-errors`: `ToolErrors` across all tools, sum over 5 minutes greater than 0, missing data not breaching.
- `<name_prefix>-k8s-read-errors` and `<name_prefix>-k8s-write-errors`: `AWS/Lambda` `Errors` of each tool Lambda, sum over 5 minutes at least 1. An invocation that times out, runs out of memory or fails to start prints no tool record, so only this alarm sees it.
- The verifier's alarms ([Verification and revert](verification.md#verification-and-revert)).

The verifier prints one JSON line per run ([Verification and revert](verification.md#verification-and-revert)). The approver prints one JSON line per decision: `{"event": "decision", "plan_id", "decision", "approver", "correlation_id", "apply_outcome"}`, where `apply_outcome` is `ok`, the apply's error code, or `null` when no apply ran. The rejection reason is not logged.
