# Roadmap

Milestone 0 is implemented and tested. In Milestone 1, items 1 (spec-level conflict detection), 2 (per-object oscillation breaker), 3 (classification drift sentinel and CI checks), 4 (measured impact preview and blast-radius budget) and 5 (server-side approval) are implemented and tested on a test cluster, and the item 6 spike (caller identity) is done. The work listed under "Later in this milestone" and the later milestones is planned and not yet implemented, and the order may change as the design is validated.

## Milestone 0: Vertical slice (done)

One read tool and a guarded `plan_scale_deployment` / `apply_scale_deployment` pair, deployed to a test cluster and approved through AWS DevOps Agent chat. See [Milestone 0](milestone-0.md) for the setup, tests and observed behavior.

## Milestone 1: Server-verified write path

**Goal:** make every write a transaction the server can verify on its own, so that a misclassified tool, a client without an approval step, or a prompt-injected agent cannot change anything without a human approval pinned to the exact arguments. Items are listed in implementation order.

### 1. Spec-level conflict detection

**Goal:** stop status-only churn from rejecting valid plans. Pin plans to `metadata.generation` and `spec.replicas` instead of `resourceVersion`; on apply, re-read the object, check both, then dry-run and patch against the current `resourceVersion`. A mismatch returns `conflict` with the planned and current values.

**Done when:** an apply during a rollout no longer fails because pods are starting, while a spec change made after the plan is still rejected and the error shows what changed.

### 2. Per-object oscillation breaker (implemented)

See [Oscillation breaker](guards.md#oscillation-breaker).

**Goal:** prevent ping-pong between agents and humans on the same object.

- Scaling in the reverse direction within a configurable window is rejected as flapping.
- A per-object hourly apply budget, a global hourly apply budget and a per-object cooldown between applies.
- If the object was changed by someone else since the last kube-handrail apply, undoing that change requires an explicit override pinned in the plan arguments.

**Done when:** a scale-up followed shortly by a scale-down of the same Deployment is rejected, a manual change is not reverted without the override, and each rejection explains the recent history.

### 3. Classification drift sentinel and CI checks (implemented)

See [Drift sentinel](../mcp/CONTRACT.md#drift-sentinel). Implemented: `mcp/classification.json` as the single classification source, `scripts/check_contract.py` and the GitHub Actions workflow, and the scheduled sentinel with its alert topic and alarms. On a test deployment, reclassifying the write tool as `READ_ONLY` in the DevOps Agent association was reported as `not_mutative` drift on the next run, the kill switch was turned off and the alert published; `enable-write` restored the classification, the next run was `ok`, and the kill switch was turned back on by hand. A simulated API outage (`unknown`, kill switch untouched) is covered by unit tests only.

**Goal:** a write tool that loses its `MUTATIVE` classification must stop working, not start running without approval.

- A scheduled check reads the DevOps Agent association and the gateway tool list. If any write tool is not `MUTATIVE`, the tool set is unexpected, or directed actions are in an unexpected state, it turns the kill switch off and raises an alert. Restoring requires re-running the verified registration step.
- Fail closed on drift, but a transient API error is reported as unknown, not as drift, so the sentinel does not flap the kill switch.
- CI checks that `tools.json`, Terraform target schemas and the registered `toolDetails` agree, that every tool has an explicit classification, and that a write tool is never `READ_ONLY`.

**Done when:** downgrading a write tool to `READ_ONLY` turns the kill switch off within minutes and raises an alert, a simulated API outage does not, and CI fails on a write tool marked `READ_ONLY`.

### 4. Measured impact preview and blast-radius budget (implemented)

See [Guards](guards.md) and [Impact preview](../mcp/CONTRACT.md#impact-preview). On a test deployment, a plan reported the Service selecting the target with its ready and projected endpoints and the matching PodDisruptionBudget, a scale-down past the budget's headroom was planned with a `pdb_headroom` warning, and a 1 -> 3 plan was refused with `blast_radius_exceeded`. `scale_to_zero` and `service_without_endpoints` are covered by unit tests only, because the test deployment's minimum is 1 replica.

**Goal:** show the approver measured facts, not prose, and cap the size of any single change regardless of approval.

- Plans include the number of affected pods, PodDisruptionBudget headroom, the projected endpoint count of Services selecting the target (flagging any that would drop to zero), and warnings.
- A blast-radius budget (`MAX_REPLICA_STEP_PERCENT`, the maximum change in replicas relative to the current count) is enforced by the guard on plan and apply, independently of approval.
- The reader identity gains read access to PDBs, Services and EndpointSlices, in allowed namespaces only. It needs no access to pods: ready counts come from the Deployment status and EndpointSlices.

**Done when:** a plan that would violate a PDB or empty a Service says so in its output, and a plan over the budget is rejected even if a human would approve it.

### 5. Server-side approval (implemented)

See [Server-side approval](../mcp/CONTRACT.md#server-side-approval). Implemented and unit-tested: `APPROVAL_MODE`, the extended consume condition, `request_approval` with an SNS notification and per-plan and global limits, split TTLs, and the approver Lambda behind an IAM-authenticated Function URL with `scripts/approve.py`. On a test deployment:

- In `server` mode (`tests/e2e/e2e_gateway.py --approval-mode server`), an apply without approval returned `approval_required`. A second `request_approval` within the cooldown notified nobody. The approval URL refused a Cognito token and an unsigned request with 403. A SigV4-signed approval by the listed approver applied the plan and the record names the approver. The applied plan could not be approved again, and a rejected plan could not be applied.
- A DevOps Agent investigation started from a backlog task planned the change and called `request_approval`; the write Lambda was not invoked. The plan was rejected through `scripts/approve.py`.
- In `client` mode, the DevOps Agent chat flow (plan, chat approval, apply) worked unchanged with a single approval.
- `both` mode with a DevOps Agent chat was not tested.

**Goal:** implement [ADR 0002](adr/0002-server-verified-approval.md).

- Plan lifecycle `planned -> awaiting_approval -> approved -> applied | rejected | expired`.
- Apply consumes a plan only if an approval record exists for the same arguments and the approver differs from the requester, checked in the same conditional write.
- Approvals come only from an authenticated human channel outside MCP. Implemented: an IAM-authenticated approval API and CLI, with MFA through the approvers' identity policy. A Slack interactive message with a verified signature is a later channel. Machine identities can never approve. Approval rights are limited to an explicit approver list.
- `APPROVAL_MODE = client | server | both`; `request_approval(plan_id)` as a `READ_ONLY` tool with server-configured destinations and rate limiting; split TTLs; error code `approval_required`.

**Done when:** in `server` mode an apply without an approval is rejected with `approval_required`, an approval attempt with a machine token is rejected, a plan approved through the approval API is applied and its record names the approver; in `client` mode the DevOps Agent chat flow works unchanged with a single approval; and an autonomous investigation can plan and request approval but still cannot call the write tool itself. That last point is DevOps Agent behavior observed in Milestone 0 (it cancels `MUTATIVE` calls outside chat), not something this repository enforces; the server's part is that `request_approval` is `READ_ONLY` and that in `server` mode the approval channel runs the apply.

### 6. Caller identity spike (done)

Verified on a test deployment with the experimental interceptor in `mcp/interceptor/` (off by default; see [Caller identity](../mcp/CONTRACT.md#caller-identity-experimental)):

- A request interceptor with `passRequestHeaders` receives the validated bearer token and can rewrite `tools/call` arguments. The tool Lambda received the token's `client_id`.
- A client that sent the reserved argument itself (`attacker`) still arrived as its real `client_id`, because the interceptor overwrites the argument unconditionally.
- DevOps Agent's calls arrived under its own Cognito client and the test client's under its own, so callers can be told apart per OAuth client.
- An argument outside a tool's `inputSchema` was not rejected by the gateway.
- Per the AWS documentation, a gateway has at most one REQUEST and one RESPONSE interceptor. Policy (Cedar) evaluates the request after the REQUEST interceptor has transformed it ([AWS blog](https://aws.amazon.com/blogs/machine-learning/secure-ai-agents-with-policy-and-lambda-interceptors-in-amazon-bedrock-agentcore-gateway/)); Policy is not deployed here, so that order was not observed.
- Fallback if interceptors are unavailable: one OAuth client per agent identity, with one gateway per client. Identity is then known from which gateway was called, at the cost of more gateways.

The identity is per OAuth client, not per agent session: two agents sharing a client are indistinguishable.

**Goal:** find out whether per-agent identity can reach the write path, as a prerequisite for per-agent budgets and attribution in Milestone 2.

- Determine whether a gateway request interceptor runs before or after policy evaluation, and whether it can add verified caller claims to the tool payload.
- Runs in parallel with items 1-5; nothing in this milestone depends on it.

**Done when:** the interceptor question in "Open questions" is closed as either verified or not supported, with a fallback recorded.

### Later in this milestone

- CI: linting and policy validation on every change (unit tests, the contract check and Terraform formatting and validation run since item 3).
- Correlation ID across gateway, Lambda, DynamoDB and Kubernetes, with basic self-observability.
- Tool output hygiene for future log and event tools: size limits, control-character stripping, secret redaction, untrusted-content delimiters.
- DevOps Agent skills and an `AGENTS.md` describing the tool contract.
- Infrastructure as code that deploys cleanly to a new account, including clusters with a private endpoint.

**Done when (milestone):** all item criteria above are met, a fresh deployment works against a private-endpoint cluster, and a prompt-injection string in tool output is returned wrapped and truncated without leading to a plan.

## Milestone 2: Verified changes, more actions, accountable agents

**Goal:** confirm that changes worked, extend the action set, and make every change attributable and provable.

- **Verify and revert.** A `READ_ONLY` tool reports the outcome of an applied plan (`healthy`, `degraded`, `unknown`) from an asynchronous check. Optionally, revert-on-failure and the verification window are part of the approved arguments, so the human approves the change and its revert together. Reverts use the spec-level precondition; `unknown` never triggers a revert; a revert is never reverted; the kill switch also stops reverts.
- **Rollback by exact revision, and restart.** Rollback targets a specific revision, pins the pod template and image of that revision, and shows the image change per container. Restart follows the same plan/apply pattern.
- **Per-agent identity, budgets and kill switch.** Plans record the requesting agent; per-agent hourly budgets and a per-agent kill switch. Builds on the caller identity interceptor (Milestone 1, item 6), which identifies callers per OAuth client.
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
- **Persistence of tool classification.** Whether `MUTATIVE` / `READ_ONLY` classifications survive infrastructure-as-code updates of the MCP server association, given that unclassified tools default to `READ_ONLY`. Partly answered: a Terraform update of a gateway target (a changed tool description) left the association's classifications unchanged; an update of the association itself replaces its whole configuration, which is why `enable-write` writes every tool explicitly and the drift sentinel watches it. ([Directed actions](https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-working-with-directed-actions.html))
- **Conditional Cedar policies and tool listing.** How argument-conditioned `permit` and `forbid` policies affect which tools appear in `tools/list`. ([Action-scoped policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-action-resource.html))
- **DevOps Agent tool limits.** The per-call timeout and response size limit for custom MCP tools, which determine output and time budgets.
- **Flux ownership detection.** Which labels or annotations reliably identify Flux-managed objects, so they can be blocked like Argo CD-managed ones.
- **DevOps Agent approval reference.** Whether the approval reference that DevOps Agent requires for `MUTATIVE` calls reaches the gateway. If it does, it could be recorded as evidence; it would not be used as a gate ([ADR 0002](adr/0002-server-verified-approval.md)).

Answered by Milestone 1, item 6: a gateway REQUEST interceptor can pass verified caller claims (the OAuth `client_id`) to the tool Lambdas, and a client cannot spoof them. A gateway has one REQUEST and one RESPONSE interceptor at most ([Interceptor types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html)).

Answered by Milestone 0: how DevOps Agent treats tool classification. A `MUTATIVE` tool requires a chat approval pinned to the exact arguments and is cancelled outside chat; a write tool classified `READ_ONLY` runs without approval, within the server-side limits.
