# Roadmap

Milestone 0 is implemented and tested. Everything from Milestone 1 onward is planned and not yet implemented; the order will change as the design is validated.

## Milestone 0: Vertical slice (done)

One read tool and a guarded `plan_scale_deployment` / `apply_scale_deployment` pair, deployed to a test cluster and approved through AWS DevOps Agent chat. See [Milestone 0](milestone-0.md) for the setup, tests and observed behavior.

## Milestone 1: Server-verified write path

**Goal:** make every write a transaction the server can verify on its own, so that a misclassified tool, a client without an approval step, or a prompt-injected agent cannot change anything without a human approval pinned to the exact arguments. Items are listed in implementation order.

### 1. Spec-level conflict detection

**Goal:** stop status-only churn from rejecting valid plans. Pin plans to `metadata.generation` and `spec.replicas` instead of `resourceVersion`; on apply, re-read the object, check both, then dry-run and patch against the current `resourceVersion`. A mismatch returns `conflict` with the planned and current values.

**Done when:** an apply during a rollout no longer fails because pods are starting, while a spec change made after the plan is still rejected and the error shows what changed.

### 2. Per-object oscillation breaker

**Goal:** prevent ping-pong between agents and humans on the same object.

- Scaling in the reverse direction within a configurable window is rejected as flapping.
- A per-object hourly apply budget, added together with the global hourly budget and cooldown described in the design.
- If the object was changed by someone else since the last kube-handrail apply, undoing that change requires an explicit override pinned in the plan arguments.

**Done when:** a scale-up followed shortly by a scale-down of the same Deployment is rejected, a manual change is not reverted without the override, and each rejection explains the recent history.

### 3. Classification drift sentinel and CI checks

**Goal:** a write tool that loses its `MUTATIVE` classification must stop working, not start running without approval.

- A scheduled check reads the DevOps Agent association and the gateway tool list. If any write tool is not `MUTATIVE`, the tool set is unexpected, or directed actions are in an unexpected state, it turns the kill switch off and raises an alert. Restoring requires re-running the verified registration step.
- Fail closed on drift, but a transient API error is reported as unknown, not as drift, so the sentinel does not flap the kill switch.
- CI checks that `tools.json`, Terraform target schemas and the registered `toolDetails` agree, that every tool has an explicit classification, and that a write tool is never `READ_ONLY`.

**Done when:** downgrading a write tool to `READ_ONLY` turns the kill switch off within minutes and raises an alert, a simulated API outage does not, and CI fails on a write tool marked `READ_ONLY`.

### 4. Measured impact preview and blast-radius budget

**Goal:** show the approver measured facts, not prose, and cap the size of any single change regardless of approval.

- Plans include the number of affected pods, PodDisruptionBudget headroom, the projected endpoint count of Services selecting the target (flagging any that would drop to zero), and warnings.
- A blast-radius budget (for example, maximum relative change in replicas) is enforced by the guard independently of approval.
- The reader identity gains read access to pods, PDBs, Services and EndpointSlices, in allowed namespaces only.

**Done when:** a plan that would violate a PDB or empty a Service says so in its output, and a plan over the budget is rejected even if a human would approve it.

### 5. Server-side approval

**Goal:** implement [ADR 0002](adr/0002-server-verified-approval.md).

- Plan lifecycle `planned -> awaiting_approval -> approved -> applied | rejected | expired`.
- Apply consumes a plan only if an approval record exists for the same arguments and the approver differs from the requester, checked in the same conditional write.
- Approvals come only from an authenticated human channel outside MCP (Slack interactive message with verified signature; CLI/API with human identity and MFA). Machine identities can never approve. Approval rights are limited to an explicit approver group.
- `APPROVAL_MODE = client | server | both`; `request_approval(plan_id)` as a `READ_ONLY` tool with server-configured destinations and rate limiting; split TTLs; error code `approval_required`.

**Done when:** in `server` mode an apply without an approval is rejected with `approval_required`, an approval attempt with a machine token is rejected, a plan approved from Slack is applied and its record names the approver; in `client` mode the DevOps Agent chat flow works unchanged with a single approval; and an autonomous investigation can plan and request approval but still cannot call the write tool itself.

### 6. Caller identity spike (parallel)

**Goal:** find out whether per-agent identity can reach the write path, as a prerequisite for per-agent budgets and attribution in Milestone 2.

- Determine whether a gateway request interceptor runs before or after policy evaluation, and whether it can add verified caller claims to the tool payload.
- Runs in parallel with items 1-5; nothing in this milestone depends on it.

**Done when:** the interceptor question in "Open questions" is closed as either verified or not supported, with a fallback recorded.

### Later in this milestone

- CI: formatting, linting, unit tests and policy validation on every change.
- Correlation ID across gateway, Lambda, DynamoDB and Kubernetes, with basic self-observability.
- Tool output hygiene for future log and event tools: size limits, control-character stripping, secret redaction, untrusted-content delimiters.
- DevOps Agent skills and an `AGENTS.md` describing the tool contract.
- Infrastructure as code that deploys cleanly to a new account, including clusters with a private endpoint.

**Done when (milestone):** all item criteria above are met, a fresh deployment works against a private-endpoint cluster, and a prompt-injection string in tool output is returned wrapped and truncated without leading to a plan.

## Milestone 2: Verified changes, more actions, accountable agents

**Goal:** confirm that changes worked, extend the action set, and make every change attributable and provable.

- **Verify and revert.** A `READ_ONLY` tool reports the outcome of an applied plan (`healthy`, `degraded`, `unknown`) from an asynchronous check. Optionally, revert-on-failure and the verification window are part of the approved arguments, so the human approves the change and its revert together. Reverts use the spec-level precondition; `unknown` never triggers a revert; a revert is never reverted; the kill switch also stops reverts.
- **Rollback by exact revision, and restart.** Rollback targets a specific revision, pins the pod template and image of that revision, and shows the image change per container. Restart follows the same plan/apply pattern.
- **Per-agent identity, budgets and kill switch.** Plans record the requesting agent; per-agent hourly budgets and a per-agent kill switch. Depends on the outcome of the caller identity spike.
- **Tamper-evident audit ledger and evidence export.** Append-only audit records, hash-chained and signed, linking requesting agent, approving human and executing role; per-plan evidence export. The executor cannot rewrite its own records.
- **Single-source policy.** One policy file compiled to Cedar at the gateway, ValidatingAdmissionPolicy in the cluster, and guard configuration, with a CI check that all three agree on the same test cases. Cedar rolls out in log-only mode and admission policy in audit mode before enforcement ([Policy enforcement modes](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-enforcement-modes.html), [ValidatingAdmissionPolicy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/)). Includes change-freeze windows enforced by the guard and a break-glass path that requires a ticket reference and a second approver.

**Done when:** a degraded change is detected and, if approved with revert, reverted; a rollback to a named revision applies exactly that template; each change can be exported with its full approval chain; and the three policy layers agree on the shared test cases.

## Milestone 3: Prometheus SLO tools and alert-triggered investigations

**Goal:** fill the gap for self-hosted Prometheus and start investigations from existing alerts.

- SLO status with multi-window burn rate, golden signals and firing alerts, returning an explicit "unknown" when Prometheus is unreachable.
- An Alertmanager-to-DevOps Agent webhook trigger with deduplication, signing, throttling and a sanitized snapshot. ([Webhook docs](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-invoking-devops-agent-through-webhook.html))

**Done when:** burn-rate math passes golden-value tests, and a crash-looping workload starts one investigation (not several) shortly after its alert fires.

## Milestone 4: Node triage and change correlation

**Goal:** summarizing read tools that help an agent connect symptoms to causes.

- Node triage: unhealthy nodes, conditions, taints, resource pressure, failing pods.
- Change correlation: recent rollouts, image changes and GitOps history in one timeline.

**Done when:** nodes under pressure or tainted are summarized correctly, and a bad rollout appears in the timeline before the alert it caused.

## Later ideas

Not committed; listed for discussion.

- **GitOps pull-request path.** For GitOps-managed objects, open a minimal pull request that passes the same bounds and policy checks, instead of returning the `gitops_managed` error. Merge is the approval, sync is the apply. Argo CD first, Flux later.
- **Standing approvals (off by default).** Pre-approved, expiring, budgeted grants for a narrow, repeated change, requiring the oscillation breaker and verify-and-revert. Would change a central non-goal and needs its own ADR.
- An example thin client and a scenario/eval suite with fault injection.
- Multi-cluster support with a cluster allowlist in policy.
- A published cost breakdown.

## Open questions

These are unverified and may change the design.

- **Gateway authentication from DevOps Agent.** OAuth client credentials works (Milestone 0). Whether DevOps Agent's SigV4 signing works against an AgentCore Gateway with IAM inbound authorization is untested. ([Connecting MCP servers](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-mcp-servers.html))
- **Persistence of tool classification.** Whether `MUTATIVE` / `READ_ONLY` classifications survive infrastructure-as-code updates of the MCP server association, given that unclassified tools default to `READ_ONLY`. ([Directed actions](https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-working-with-directed-actions.html))
- **Conditional Cedar policies and tool listing.** How argument-conditioned `permit` and `forbid` policies affect which tools appear in `tools/list`. ([Action-scoped policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-action-resource.html))
- **DevOps Agent tool limits.** The per-call timeout and response size limit for custom MCP tools, which determine output and time budgets.
- **Flux ownership detection.** Which labels or annotations reliably identify Flux-managed objects, so they can be blocked like Argo CD-managed ones.
- **Gateway interceptors.** Whether a gateway supports more than one request or response interceptor, whether interceptors run before or after policy evaluation, and whether a request interceptor can add verified caller claims to the tool payload. ([Interceptor types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html))
- **DevOps Agent approval reference.** Whether the approval reference that DevOps Agent requires for `MUTATIVE` calls reaches the gateway. If it does, it could be recorded as evidence; it would not be used as a gate ([ADR 0002](adr/0002-server-verified-approval.md)).

Answered by Milestone 0: how DevOps Agent treats tool classification. A `MUTATIVE` tool requires a chat approval pinned to the exact arguments and is cancelled outside chat; a write tool classified `READ_ONLY` runs without approval, within the server-side limits.
