# Milestones done

What each finished milestone built and how it was tested. The rules are in [`mcp/CONTRACT.md`](../../mcp/CONTRACT.md); what comes next is in the [roadmap](../roadmap.md). "A test cluster" is a shared EKS test cluster; counts are end-to-end cases passed.

## Milestone 0: Vertical slice

`get_workload_status` and a guarded `plan_scale_deployment` / `apply_scale_deployment` pair behind an AgentCore Gateway, with Terraform, namespace-scoped RBAC and AWS DevOps Agent registration. Tested on a test cluster.

Observed in DevOps Agent: a `MUTATIVE` apply becomes an approval request pinned to the exact arguments (`argument_pins`); the resume needs `userActionResponse: APPROVAL_ACTION` and the option's `buttonText`; a replayed approval does not run the tool again. Outside chat the `MUTATIVE` call is cancelled before it reaches the gateway. A write tool classified `READ_ONLY` by mistake runs without approval, within the server-side limits. OAuth client credentials work for the gateway.

## Milestone 1: Server-verified write path

1. **Spec-level conflict detection.** Plans pin `metadata.generation`, `uid` and `spec.replicas` instead of `resourceVersion`, so status churn during a rollout no longer causes `conflict`.
2. **Oscillation breaker.** Flapping, cooldown, per-object and global budgets, manual-change override.
3. **Classification drift sentinel and CI checks.** `mcp/classification.json` as the one source, `scripts/check_contract.py`, the scheduled sentinel. A write tool reclassified `READ_ONLY` was reported as `not_mutative` on the next run, the kill switch was turned off and the alert published; `enable-write` restored it.
4. **Impact preview and blast-radius budget.** A plan reported the Service and PodDisruptionBudget in front of the target; a scale-down past the headroom warned `pdb_headroom`; 1 -> 3 was `blast_radius_exceeded`.
5. **Server-side approval** ([ADR 0002](../adr/0002-server-verified-approval.md)). In `server` mode an apply without approval was `approval_required`, the approval URL refused a Cognito token and an unsigned request, and a signed approval applied the plan and named the approver. A DevOps Agent investigation planned and called `request_approval` without invoking the write Lambda. `both` mode with a DevOps Agent chat was not tested then.
6. **Caller identity spike.** A REQUEST interceptor with `passRequestHeaders` sees the validated token and can overwrite `tools/call` arguments, so a client cannot spoof its `client_id`. A gateway has at most one REQUEST and one RESPONSE interceptor. Identity is per OAuth client, not per session.

Also: CI (ruff, unit tests, contract check, Terraform validation); correlation IDs with a tool record and metrics; tool output hygiene with `get_workload_events` (an injected Event came back wrapped, truncated, redacted and stripped, and a DevOps Agent chat reported it as an injection attempt and planned nothing); `AGENTS.md` and a skill; deployment to new accounts and private-endpoint clusters (a fresh deployment against a cluster with only a private endpoint, reaching AWS through VPC endpoints, passed the smoke and guard tests, the sentinel and a DevOps Agent chat with approval; deleting the Lambda security group waited about 20 minutes for Lambda to release its network interfaces). Not run: a brand-new AWS account.

## Milestone 2: Verified changes, more actions, accountable agents

1. **Action framework and restart.** One generic plan/apply path; records store the exact argument payload the approval pins; plans pin the pod template hash. A restart replaced every pod, kept the count and left the plan ID on every new pod; a DevOps Agent chat planned and applied one.
2. **Rollback by exact revision.** The pod template after a rollback equalled the target ReplicaSet's minus the hash label; a pruned revision was `revision_not_found`, an edited ReplicaSet or paused Deployment `conflict`, a hand-changed template `manual_change`. A `change-cause` telling the agent to scale was reported as untrusted text. `kubectl rollout undo` was confirmed to replace the Deployment's annotations with those of the restored ReplicaSet.
3. **Per-agent identity, budgets and kill switch** ([ADR 0003](../adr/0003-agent-identity.md)). Upgraded in place without replacing Cognito clients or the gateway. A client's own `_kube_handrail_caller` was ignored; a second agent could not apply or request approval for the first agent's plan. A broken interceptor made the gateway return HTTP 500 for every request (fail closed); with the interceptor detached, writes were `agent_unknown` and the sentinel reported `missing_interceptor`.
4. **Verify and revert** ([ADR 0004](../adr/0004-pre-approved-revert.md)). The `verify-due` index was built online. A change that reached its target was `healthy`, a hand scale in the window `unknown`, pods refused by a ResourceQuota `degraded` (`replica_failure` within 60 seconds) and, with revert approved, set back; the revert was verified and never reverted, and re-planning was `flapping`. With the kill switch off the revert stayed `skipped`.
5. **Change freezes and admission policy** ([ADR 0005](../adr/0005-change-freezes.md), [ADR 0006](../adr/0006-admission-policy.md)). A plan approved before a freeze was refused at apply, stayed unconsumed and applied after the freeze; an invalid freezes value refused writes; a DevOps Agent chat reported `change_frozen` and did not look for another way. With a hand-edited copy of the policy, a scale the guard allowed was refused by the cluster as `admission_denied` and the tool-errors alarm fired. The policy stays installed in `Deny`.
6. **Durable audit record** ([ADR 0007](../adr/0007-durable-audit-record.md)). Entries appeared in the bucket within seconds; an overwrite with If-None-Match was `PreconditionFailed` and a put without it was denied; IAM simulation gave `explicitDeny` for the write role on put, version delete and governance bypass.

Deferred to "Later ideas": the single policy file with Cedar and break-glass, the signed ledger, revert of a rollback, an agent-facing evidence tool, EventBridge Scheduler.

## Milestone 3: Quick wins and guard rails

GitOps ownership by controller markers (Helm releases are no longer refused, Flux-managed objects are), the plan TTL by approval mode, the plan rate limit removed, faster end-to-end runs, guidance sync tests, a release process, a dashboard and a shadow-mode recipe. Tested on a test cluster, except the test profile and `--reset-counters`. Flux markers were tested with labels set by hand, not with a running Flux.

## Milestone 4: Simplify, then make upgrades one command

Legacy mode and the `verify_enabled` switch removed with 12 `moved` blocks; `make upgrade`. From Milestone 3 the plan showed only the moves and package rebuilds, and the upgrade took 110 seconds.

## Milestone 5: CI tier 1 with kind and moto

`tests/integration` runs the guard scenarios on kind with moto, in CI on every pull request (about 2.5 minutes). The live-only end-to-end run passed 26 of 26 cases in 92 seconds, against about 15 minutes for the full run.

## Milestone 6: Several namespaces

Namespace policy with bounds, actions, approvers and GitOps markers per namespace; rendered RBAC and admission policy; the plan-time admission check. Tested on a test cluster with two namespaces in client and server mode.

## Milestone 8: Tool schema release #1

HPA adjust, `change_ref`, the approval in `get_plan_outcome`. Tested on kind with metrics-server and on a test cluster without metrics-server (`make upgrade` with one `enable-write`, the admission cases, the end-to-end run with `--with-hpa`). A DevOps Agent chat covered HPA adjust, the namespace policy, restart, rollback and `both` mode. On kind (Kubernetes 1.31.4, metrics-server v0.7.2): HPAs have no `metadata.generation`, and `resourceVersion` changes on every status write; without a metrics API, raising `minReplicas` from 1 to 2 scaled the Deployment to 2 while `ScalingActive` stayed `False`; `desiredReplicas` was 0 until the first rescale; `status.currentReplicas` trailed a patch by about 15 seconds. The rules these led to are in [Adjust HPA](../../mcp/contract/adjust-hpa.md).

## Milestone 9: Approver and executor identity

`approval_mode` on plan records, `scripts/audit.py verify`, Logs Insights templates in the export, the User-Agent with plan and correlation ID, `COMPLIANCE` recommended for production. Tested on a test cluster.

## Answered questions

- **DevOps Agent and classification** (Milestone 0): a `MUTATIVE` tool needs a chat approval pinned to its arguments and is cancelled outside chat; a write tool classified `READ_ONLY` runs without approval.
- **Verified caller claims** (Milestone 1): a REQUEST interceptor can pass the OAuth `client_id` to the tool Lambdas, and a client cannot spoof it.
- **Flux markers** (Milestone 3): the kustomize-controller and helm-controller `name` and `namespace` labels.
- **Chat approvals in CloudTrail** (Milestone 9): `UpdateApprovalAction` is a management event with the approver's IAM identity, but every tool argument, the plan ID included, is redacted and there is no approval ID. A `client`-mode approval can only be matched to a plan by time, so the audit-grade approver is the server approval.
- **Classification across updates** (partly): a Terraform update of a gateway target left the association's classifications unchanged; an update of the association replaces its whole configuration, which is why `enable-write` writes every tool and the sentinel watches it.
