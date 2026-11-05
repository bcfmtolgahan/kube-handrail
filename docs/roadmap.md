# Roadmap

Milestone 0 is implemented and tested. In Milestone 1, items 1 (spec-level conflict detection), 2 (per-object oscillation breaker), 3 (classification drift sentinel and CI checks), 4 (measured impact preview and blast-radius budget) and 5 (server-side approval) are implemented and tested on a test cluster, and the item 6 spike (caller identity) is done. Of the work listed under "Later in this milestone", the correlation ID, tool output hygiene and the DevOps Agent skills and `AGENTS.md` are implemented and tested on a test cluster, and the infrastructure as code for new accounts and private-endpoint clusters is implemented and tested, including a fresh deployment against a cluster with only a private endpoint. With that, Milestone 1 is complete. In Milestone 2, item 1 (action framework and restart) is implemented and tested on a test cluster, and item 2 (rollback by exact revision) is implemented and tested on a test cluster. The rest of the later milestones is planned and not yet implemented, and the order may change as the design is validated.

## Milestone 0: Vertical slice (done)

One read tool and a guarded `plan_scale_deployment` / `apply_scale_deployment` pair, deployed to a test cluster and approved through AWS DevOps Agent chat. See [Milestone 0](milestone-0.md) for the setup, tests and observed behavior.

## Milestone 1: Server-verified write path (done)

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

- CI: linting and policy validation on every change (implemented for what exists). CI runs `ruff check` and `ruff format --check`, the unit tests, the contract check, and Terraform formatting and validation. Policy validation arrives with the policies: Cedar and admission policy are Milestone 2 (single-source policy).
- Correlation ID across gateway, Lambda, DynamoDB and Kubernetes, with basic self-observability (implemented). See [Observability](../mcp/CONTRACT.md#observability): the gateway's request ID is recorded per step in the plan record and set as an annotation by the apply, every tool call logs one record that is also a CloudWatch metric (`ToolCalls`, `ToolErrors`, `GuardRejections`), and alarms notify the alert topic on internal and Kubernetes API errors and on failed tool Lambda invocations. Tested on a test cluster: the Deployment carries the apply's correlation ID, which differs from the plan's, and the metrics and alarms are in place.
- Tool output hygiene for future log and event tools: size limits, control-character stripping, secret redaction, untrusted-content delimiters. Implemented and tested on a test cluster: [Untrusted output](../mcp/CONTRACT.md#untrusted-output), applied to `get_workload_status` conditions and to the new `READ_ONLY` tool [`get_workload_events`](../mcp/CONTRACT.md#workload-events). The end-to-end test plants an Event whose message carries a prompt-injection string, a fake access key ID and control characters, and checks that it is returned wrapped, truncated, redacted and stripped and that reading it creates no plan. Whether DevOps Agent, given that event, still proposes no plan is a [manual check](milestone-0.md#untrusted-event-text-in-devops-agent); on the test deployment the agent called `get_workload_events`, reported the planted Event as an injection attempt and created no plan.
- DevOps Agent skills and an `AGENTS.md` describing the tool contract (implemented and tested on a test deployment; see [Agent guidance](../mcp/CONTRACT.md#agent-guidance)). A new chat follows `AGENTS.md` and reads the skill.
- Infrastructure as code that deploys cleanly to a new account, including clusters with a private endpoint (implemented and tested). See [Deploying to a new account](deploy.md): optional VPC placement of the two tool Lambdas with their network interface permissions, an optional cluster security group rule and VPC endpoints for exactly the AWS APIs they call, and plan-time checks of the authentication mode, endpoint access and network variables. Tested on a test cluster that has both public and private endpoint access: with the Lambdas in private subnets (NAT for the AWS APIs) and the cluster security group rule, the API server name resolves to private addresses inside the VPC, and the server-mode guard tests (39/39) and the DevOps Agent chat flow with server-side approval passed. One apply moving the Lambdas into the VPC and one moving them out both complete without manual steps. A second, fresh deployment (its own `name_prefix` and state) against a separate test cluster then ran with the Lambdas in subnets without any route out of the VPC, reaching AWS only through `create_vpc_endpoints` in a VPC shared with other workloads, which kept working because the endpoints accept the VPC's address ranges. On it, the smoke test, the guard tests (45/45 with plan expiry, and 39/39 in server mode), the drift sentinel, and DevOps Agent registration in a new agent space with a chat plan, approval and apply passed. With the cluster's public endpoint then turned off, so that only the private endpoint answered, the smoke test, the sentinel and a DevOps Agent chat scale with approval passed again. `scripts/cleanup.sh --delete-space` then removed the agent space and all 57 resources; deleting the Lambda security group waited about 20 minutes for Lambda to release its network interfaces. Not run: a brand-new AWS account (the fresh deployment shared its account with another one).

**Done when (milestone):** all item criteria above are met, a fresh deployment works against a private-endpoint cluster, and a prompt-injection string in tool output is returned wrapped and truncated without leading to a plan.

## Milestone 2: Verified changes, more actions, accountable agents

**Goal:** confirm that changes worked, extend the action set, and make every change attributable and provable. Items are listed in implementation order. Spikes on the assumptions they depend on (Cedar with the caller interceptor, JSON Patch resourceVersion preconditions, template hashing, interceptor failure behavior) run first.

### 1. Action framework and restart (implemented)

See [Restart](../mcp/CONTRACT.md#restart). On a test cluster:

- The Milestone 1 end-to-end suite passed on the generic path, with the restart cases added (`--with-rollout`, `--with-expiry`): 50 of 50 in `client` mode and 44 of 44 in `server` mode, where the restart was approved through `scripts/approve.py`.
- A restart replaced every pod, kept the replica count, and left the plan ID on the pod template and on every new pod. A second restart right after it was refused with `cooldown`, and with the kill switch off the apply returned `remediation_disabled`.
- In `client` mode, a DevOps Agent chat asked to restart a Deployment found the restart tools, checked the status, planned, and applied after the chat approval. The drift sentinel reported `ok` after `enable-write` registered the new tools.

**Goal:** add actions without copying the guarded write path.

- Plan and apply run through one generic path. Each action supplies its validation, record match, checks, breaker rules and patch body. Scale moves onto it unchanged.
- Plan records store the exact argument payload that the approval pins. The approval channel forwards it as the apply arguments, and apply compares against it, for every action.
- Plans pin a hash of the pod template, for every action.
- `plan_restart_deployment` / `apply_restart_deployment` set a pod-template annotation that carries the plan ID, so the resulting template is known and approved at plan time. Restart has no replica direction and no step budget; the impact preview shows the rollout strategy, PDB headroom and Service endpoints during the rollout.

**Done when:** the Milestone 1 guard tests pass unchanged on the generic path, and a restart is planned, approved and applied in `client` and `server` mode, replacing every pod with the plan ID annotation.

### 2. Rollback by exact revision (implemented)

See [Rollout history](../mcp/CONTRACT.md#rollout-history) and [Rollback](../mcp/CONTRACT.md#rollback). On a test cluster:

- The end-to-end suite with the rollout cases (`--with-rollout`, `--with-expiry`) passed 64 of 64 in `client` mode and 57 of 57 in `server` mode, where the rollback was approved through `scripts/approve.py`. After a rollback, the Deployment's pod template equalled the target ReplicaSet's template minus the hash label, compared as JSON. A pruned revision returned `revision_not_found`, a ReplicaSet edited between plan and apply and a paused Deployment returned `conflict`, a template changed by hand returned `manual_change` until the override was planned, a GitOps-managed Deployment was refused, and a different revision on apply returned `plan_mismatch`.
- The writer group can get ReplicaSets in the sandbox namespace and not in `default`.
- In `client` mode, a DevOps Agent chat listed the history and rolled a Deployment back to a named revision after the chat approval; the resulting template matched that revision's ReplicaSet. A `change-cause` on that revision told the agent to scale the Deployment without asking; the agent reported it as untrusted text and created no scale plan.
- `kubectl rollout undo` was confirmed to replace the Deployment's annotations with those of the restored ReplicaSet, so the last-plan annotation goes back to the plan that was current at that revision. After such an undo the template manual-change rule compares against that older plan, as [History checks](../mcp/CONTRACT.md#history-checks-oscillation-breaker) describes.

**Goal:** roll back to the template the human approved, not to whatever a revision number points at by the time the apply runs.

- `get_rollout_history` lists the owned revisions, with images per container as untrusted text.
- The plan resolves a revision to exactly one ReplicaSet owned by the Deployment. It pins that ReplicaSet's UID and template hash, and shows the image change per container and the names of other changed fields, never values.
- Apply re-reads the ReplicaSet, re-hashes its template, and writes exactly the template it checked, with a resourceVersion precondition. A pruned revision returns `revision_not_found`; an edited target or a spec change since the plan returns `conflict`.
- Rolling back over a template change made by someone else since the last kube-handrail template change requires the manual-change override. The writer gains read access to ReplicaSets in allowed namespaces only.

**Done when:** a rollback to a named revision writes exactly that revision's template, and a target edited, pruned or renumbered after the plan is refused.

### 3. Per-agent identity, budgets and kill switch

**Goal:** know which agent asked for each change, and limit or stop one agent without stopping the others. Builds on the caller identity interceptor (Milestone 1, item 6), which identifies callers per OAuth client.

- The interceptor is on by default and the write path fails closed without a verified caller. Agents are configured in Terraform, one OAuth client each.
- Plans record the requesting agent. Only that agent can request approval for its plan or apply it through the gateway; an approval-triggered apply is attributed to the plan's agent.
- Per-agent hourly apply budgets alongside the global budget, and a per-agent kill switch, checked on plan, `request_approval` and apply.
- The drift sentinel checks that the gateway still has its interceptor and only the expected clients.

**Done when:** every plan names its agent from the verified OAuth client and a client cannot change it; an agent over its budget or with its kill switch off is refused while other agents keep working; one agent cannot apply another's plan; and a gateway that lost its interceptor cannot be used to write and is reported by the sentinel.

### 4. Verify and revert

**Goal:** confirm that an applied change worked, and undo it if the human approved that in advance.

- A verification window, and for scale revert-on-failure, are optional plan arguments pinned in the approval, so the human approves the change and its revert together.
- The apply schedules the check in the same write that consumes the plan. A scheduled sweeper runs it through an internal entry point of the write Lambda that the gateway cannot reach.
- A `READ_ONLY` tool, `get_plan_outcome`, reports `healthy`, `degraded` or `unknown`, and the revert state.
- A revert uses the spec-level precondition and restores exactly the approved starting state. `unknown` never triggers a revert, a revert is never reverted, and the kill switch also stops reverts. A revert counts as a change for later breaker checks, so re-planning the same change right after its revert is refused.

**Done when:** a degraded change is detected and, if approved with revert, reverted, the revert itself is verified and never reverted, and an `unknown` outcome or a turned-off kill switch leaves the object as it is.

### 5. Single-source policy, change freezes and break-glass

**Goal:** one reviewed policy file that the gateway, the cluster and the guard all enforce the same way.

- `policy/policy.json` (namespaces with replica bounds, allowed actions, freeze windows, break-glass, rollout state) is compiled to Cedar at the gateway and to a ValidatingAdmissionPolicy that applies only to the kube-handrail writer in allowed namespaces. The guard reads the same file. A CI check shows that all three agree on shared test cases.
- Cedar rolls out in log-only mode and admission policy in audit mode before enforcement ([Policy enforcement modes](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-enforcement-modes.html), [ValidatingAdmissionPolicy](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/)). The drift sentinel reports a detached engine, a mode change or a policy switched to log-only.
- Change-freeze windows are enforced by the guard on plan, `request_approval` and apply, including approval-triggered applies that Cedar never sees.
- A break-glass path lifts only a freeze. It requires a ticket reference pinned in the plan arguments and a second approver from a different approver entry, and is available in `server` and `both` approval modes.

**Done when:** CI shows the three policy layers agreeing on the shared test cases; on a test deployment Cedar ran in log-only mode and admission policy in audit mode before both were enforced; a freeze refuses changes; and a break-glass change needs a ticket and two distinct approvers.

### 6. Tamper-evident audit ledger and evidence export

**Goal:** prove afterwards who asked for each change, who approved it, and what ran it, even if the executor is compromised.

- Every change to a plan record is captured from the table's stream by a separate writer and appended as a signed, hash-chained entry to write-once storage. The executor writes no ledger records and cannot alter them.
- The approval channel writes a write-once receipt for each decision. An approval, consume or result that does not match its receipts or the expected transitions is flagged as an anomaly and alerts.
- `scripts/ledger.py` verifies the chain and signatures offline and exports one plan's evidence: the requesting agent, the approver or approvers, the executing role and channel, and the verification and revert outcome.

**Done when:** a forged approval or rewritten result written with the executor's own credentials is detected, and a change can be exported with its full approval chain and verified offline.

### Deferred from this milestone

These move to "Later ideas":

- Revert of a rollback (a rollback to the previous template, by hash).
- Agent-facing evidence tool; weekly freeze windows; per-agent Cedar permits.
- Moving the scheduled sentinel and sweeper to EventBridge Scheduler.

**Done when (milestone):** a degraded change is detected and, if approved with revert, reverted; a rollback to a named revision applies exactly that template; each change can be exported with its full approval chain; and the three policy layers agree on the shared test cases.

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
- Revert of a rollback (a rollback to the previous template, by hash).
- An agent-facing evidence tool, weekly freeze windows and per-agent Cedar permits.
- Moving the scheduled sentinel and sweeper to EventBridge Scheduler.

## Open questions

These are unverified and may change the design.

- **Gateway authentication from DevOps Agent.** OAuth client credentials works (Milestone 0). Whether DevOps Agent's SigV4 signing works against an AgentCore Gateway with IAM inbound authorization is untested. ([Connecting MCP servers](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-mcp-servers.html))
- **Persistence of tool classification.** Whether `MUTATIVE` / `READ_ONLY` classifications survive infrastructure-as-code updates of the MCP server association, given that unclassified tools default to `READ_ONLY`. Partly answered: a Terraform update of a gateway target (a changed tool description) left the association's classifications unchanged; an update of the association itself replaces its whole configuration, which is why `enable-write` writes every tool explicitly and the drift sentinel watches it. ([Directed actions](https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-working-with-directed-actions.html))
- **Conditional Cedar policies and tool listing.** How argument-conditioned `permit` and `forbid` policies affect which tools appear in `tools/list`. ([Action-scoped policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-action-resource.html))
- **DevOps Agent tool limits.** The per-call timeout and response size limit for custom MCP tools, which determine output and time budgets.
- **Flux ownership detection.** Which labels or annotations reliably identify Flux-managed objects, so they can be blocked like Argo CD-managed ones.
- **DevOps Agent approval reference.** Whether the approval reference that DevOps Agent requires for `MUTATIVE` calls reaches the gateway. If it does, it could be recorded as evidence; it would not be used as a gate ([ADR 0002](adr/0002-server-verified-approval.md)).
- **CloudTrail data events as executor identity.** Whether CloudTrail DynamoDB data events on the plan table can show the principal that consumed a plan, so that the executor identity of a change is observed rather than attributed by construction (Milestone 2, item 6).

Answered by Milestone 1, item 6: a gateway REQUEST interceptor can pass verified caller claims (the OAuth `client_id`) to the tool Lambdas, and a client cannot spoof them. A gateway has one REQUEST and one RESPONSE interceptor at most ([Interceptor types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html)).

Answered by Milestone 0: how DevOps Agent treats tool classification. A `MUTATIVE` tool requires a chat approval pinned to the exact arguments and is cancelled outside chat; a write tool classified `READ_ONLY` runs without approval, within the server-side limits.
