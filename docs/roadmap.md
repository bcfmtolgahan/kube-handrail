# Roadmap

Milestone 0 is implemented and tested. In Milestone 1, items 1 (spec-level conflict detection), 2 (per-object oscillation breaker), 3 (classification drift sentinel and CI checks), 4 (measured impact preview and blast-radius budget) and 5 (server-side approval) are implemented and tested on a test cluster, and the item 6 spike (caller identity) is done. Of the work listed under "Later in this milestone", the correlation ID, tool output hygiene and the DevOps Agent skills and `AGENTS.md` are implemented and tested on a test cluster, and the infrastructure as code for new accounts and private-endpoint clusters is implemented and tested, including a fresh deployment against a cluster with only a private endpoint. With that, Milestone 1 is complete. In Milestone 2, item 1 (action framework and restart) is implemented and tested on a test cluster, item 2 (rollback by exact revision) is implemented and tested on a test cluster, item 3 (per-agent identity, budgets and kill switch) is implemented and tested on a test cluster, item 4 (verify and revert) is implemented and tested on a test cluster, and items 5 (change freezes and admission policy) and 6 (durable audit record), narrowed to what a production deployment needs, are implemented and tested on a test cluster. With that, Milestone 2 is complete; the deferred parts are listed at its end. Milestone 3 is implemented and tested on a test cluster, except the end-to-end test profile and `--reset-counters`. The rest of the later milestones is planned and not yet implemented, and the order may change as the design is validated.

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

Verified on a test deployment with the then experimental interceptor in `mcp/interceptor/` (off by default at the time; it is now the basis of [Agent identity](../mcp/CONTRACT.md#agent-identity)):

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

### 3. Per-agent identity, budgets and kill switch (implemented)

See [Agent identity](../mcp/CONTRACT.md#agent-identity) and [ADR 0003](adr/0003-agent-identity.md). On a test cluster:

- The deployment upgraded in place: no Cognito client and no gateway was replaced, and DevOps Agent kept working without registering again. The first apply needs the caller key created on its own ([Upgrading](deploy.md#upgrading)).
- The end-to-end suite with the identity cases (`--with-identity`, a second client) passed 76 of 76 in `client` mode and 64 of 64 in `server` mode: plans name `agent:smoke_test`, a client's own `_kube_handrail_caller` is ignored, a second agent applying or requesting approval for the first agent's plan gets `forbidden`, a second agent with its switch off gets `remediation_disabled` and over its budget gets `agent_budget_exceeded` while the first keeps working, and in `server` mode an approval for a switched-off agent consumes the plan and is refused.
- A DevOps Agent chat restart recorded `agent:devops_agent` on the plan and in the Deployment's `kube-handrail.io/requested-by` annotation.
- An interceptor that fails (a broken handler) made the gateway return HTTP 500 for every request, `initialize` included: the request fails closed.
- With the interceptor detached from the gateway, reads still worked, a plan carrying a self-set caller (unsigned, or with a made-up signature) was refused with `forbidden` (`agent_unknown`), and the sentinel reported `missing_interceptor` and turned the kill switch off. `terraform apply` restored the gateway.

**Goal:** know which agent asked for each change, and limit or stop one agent without stopping the others. Builds on the caller identity interceptor (Milestone 1, item 6), which identifies callers per OAuth client.

- The interceptor is on by default and the write path fails closed without a verified caller. Agents are configured in Terraform, one OAuth client each.
- Plans record the requesting agent. Only that agent can request approval for its plan or apply it through the gateway; an approval-triggered apply is attributed to the plan's agent.
- Per-agent hourly apply budgets alongside the global budget, and a per-agent kill switch, checked on plan, `request_approval` and apply.
- The drift sentinel checks that the gateway still has its interceptor and only the expected clients.

**Done when:** every plan names its agent from the verified OAuth client and a client cannot change it; an agent over its budget or with its kill switch off is refused while other agents keep working; one agent cannot apply another's plan; and a gateway that lost its interceptor cannot be used to write and is reported by the sentinel.

### 4. Verify and revert (implemented)

See [Verification and revert](../mcp/CONTRACT.md#verification-and-revert) and [ADR 0004](adr/0004-pre-approved-revert.md). On a test cluster:

- The deployment upgraded in place with one `terraform apply`: the `verify-due` index was built online on the existing plan table, and no gateway or Cognito client was replaced. The verifier ran once a minute and its stalled alarm stayed `OK`.
- The end-to-end suite with the verification cases (`--with-verify`) passed 71 of 71 in `client` mode and 55 of 55 in `server` mode, where the window and `revert_on_failure` were approved through `scripts/approve.py`. A change that reached its target was `healthy`; a scale by hand in the window was `unknown` (`spec_changed`) and left alone; pods refused by a ResourceQuota made the change `degraded` and, with revert approved, it was set back to its starting count, the revert was verified `healthy` and never reverted, and planning the same change again was refused with `flapping`. With the kill switch off the revert was `skipped` and stayed skipped once the switch was back on; without `revert_on_failure` a degraded change was reported and left alone. No ResourceQuota or LimitRange was left behind.
- A ResourceQuota refusal sets the Deployment's `ReplicaFailure` condition, so the outcome reports `degraded` with `replica_failure` well within a 60-second window. The verifier's revert alarm fired for the reverts and went back to `OK`.
- In `client` mode, a DevOps Agent chat scaled a Deployment up with a 60-second window and `revert_on_failure`, applied after the chat approval with both arguments, then read `get_plan_outcome`: `healthy` (`target_reached`), revert `not_needed`. A scale back down with a window only was also verified `healthy`.

**Goal:** confirm that an applied change worked, and undo it if the human approved that in advance.

- A verification window, and for scale revert-on-failure, are optional plan arguments pinned in the approval, so the human approves the change and its revert together.
- The apply schedules the check in the same write that consumes the plan. A scheduled sweeper runs it through an internal entry point of the write Lambda that the gateway cannot reach.
- A `READ_ONLY` tool, `get_plan_outcome`, reports `healthy`, `degraded` or `unknown`, and the revert state.
- A revert uses the spec-level precondition and restores exactly the approved starting state. `unknown` never triggers a revert, a revert is never reverted, and the kill switch also stops reverts. A revert counts as a change for later breaker checks, so re-planning the same change right after its revert is refused.

**Done when:** a degraded change is detected and, if approved with revert, reverted, the revert itself is verified and never reverted, and an `unknown` outcome or a turned-off kill switch leaves the object as it is.

### 5. Change freezes and admission policy (implemented)

The scope was narrowed to the two parts a production deployment needs; the single policy file, Cedar at the gateway and break-glass are deferred (below).

**Goal:** stop changes during declared freezes, and have the cluster itself refuse a write outside the bounds even if the guard is wrong.

- Change freezes: windows in one SSM parameter (namespaces, start, until, reason, optional actions), changed by operators with `scripts/freeze.py` without a deploy. The guard checks them on every plan and again at apply with the current time, so an approval given before a freeze is not applied during it; a refused apply does not consume the plan. An unreadable or invalid value refuses writes like the kill switch. A pre-approved revert is exempt. There is no break-glass path: an operator removes the freeze. See [Change freezes](../mcp/CONTRACT.md#change-freezes) and [ADR 0005](adr/0005-change-freezes.md).
- Admission policy: a ValidatingAdmissionPolicy, `kube-handrail-writer-bounds`, refuses a writer patch that changes a Deployment's replica count to a value outside the bounds, only for the writer group and only in the sandbox namespace. It rolls out in `Warn` and `Audit` and is switched to `Deny` by the operator. The write Lambda maps its denial to `admission_denied`. CI applies it on kind (Kubernetes 1.31) and runs server-side dry-run cases (`tests/admission/run.sh`). See [Admission policy](../mcp/CONTRACT.md#admission-policy) and [ADR 0006](adr/0006-admission-policy.md).

**Done when:** a freeze refuses plans and applies, including an apply of a plan approved before the freeze; and on a test cluster the admission policy ran in audit mode, then in `Deny`, and an apply over the bounds was refused with `admission_denied` while other users and namespaces were unaffected.

Change freezes, on a test cluster:

- The deployment upgraded in place with one `terraform apply` (a new SSM parameter and in-place updates of the tool Lambdas); the smoke test passed and `scripts/freeze.py list` showed no freezes.
- The end-to-end suite with the freeze cases (`--with-freeze`) passed 48 of 48 in `client` mode and 43 of 43 in `server` mode: a freeze on another namespace left a sandbox plan `ok`; a plan in a frozen namespace was `change_frozen` with its reason and `until`; a plan made before the freeze (in `server` mode, an approval given before it) was refused at apply with `change_frozen` and stayed unconsumed, and applied once the freeze was removed. The stored freezes were the same before and after.
- An invalid freezes value refused plans with `remediation_disabled` (`scope` `freezes`, `reason` `invalid`); `scripts/freeze.py remove --all` restored it.
- In `client` mode, a DevOps Agent chat asked to scale a Deployment in a frozen namespace reported `change_frozen` with the reason and the end of the freeze, did not retry, and did not look for another way to make the change.

Admission policy, on a test cluster (Kubernetes 1.35):

- The policy type-checked without warnings. In audit mode, `tests/admission/run.sh` (bounds 1 and 3) showed a writer over the maximum admitted with a warning; in `Deny`, a writer scale within the bounds was admitted, over the maximum and below the minimum refused (also past RBAC, and through `deployments/scale`), a writer restart admitted, and another user over the maximum in the sandbox namespace admitted. The binding went back to `[Warn, Audit]` afterwards.
- With a hand-edited copy of the policy (maximum 1, `Deny`, not committed), a scale to 2 that the guard allowed was refused by the cluster: the apply returned `admission_denied` naming the policy and binding, the plan was used with `result` `admission_denied`, the Deployment was unchanged, and the tool-errors alarm fired.
- With the real bounds in `Deny`, the end-to-end suite with the restart and rollback cases passed 63 of 63, and the other applications on the cluster stayed healthy. The policy stays installed in `Deny`.

### 6. Durable audit record and evidence export (implemented)

The scope was narrowed to a write-once record outside the executor's reach; signing, hash chaining, approval receipts and automatic anomaly detection are deferred (below). See [Audit record](../mcp/CONTRACT.md#audit-record) and [ADR 0007](adr/0007-durable-audit-record.md).

**Goal:** show afterwards who asked for each change, who approved it, and how it ran, in a record the executor cannot alter.

- Every change to a plan record is captured from the table's stream by a separate Lambda and written once, as one object, to an S3 bucket with Object Lock. The tool Lambdas, the approver and the verifier are denied any write to the bucket.
- A redelivered record maps to the same key and bytes; an existing key with other content stops the shard and alarms. Failed records go to a dead-letter queue, with alarms on it, the iterator age and the Lambda's errors.
- `scripts/audit.py export` gives one plan's entries and a summary: the requesting agent, the approver, the channel, the result, and the verification and revert outcome, following the link to a revert record.

**Done when:** a change can be exported with its approval chain after its plan record has expired, and an overwrite of an entry is refused.

On a test cluster:

- The deployment upgraded in place with one `terraform apply`: the stream was turned on for the existing plan table, and the bucket, the audit Lambda, its event source mapping, the dead-letter queue and the alarms were created. No gateway or Cognito client was replaced.
- The end-to-end suite with the verification and audit cases (`--with-verify --with-audit`) passed 75 of 75: a plan's creation and its consume appeared in the bucket within seconds, the export named the requesting agent, the result and the channel, an overwrite of an entry with If-None-Match was `PreconditionFailed`, and a put without it was denied by the bucket policy.
- The export of a scale that was verified `degraded` and reverted showed the verification, the revert with its revert plan ID, and the revert record's own entries.
- IAM policy simulation with the bucket policy gave `explicitDeny` for the write role on `s3:PutObject`, `s3:DeleteObjectVersion` and `s3:BypassGovernanceRetention`; the read role, the approver and the verifier have no permission on the bucket.

### Deferred from this milestone

These move to "Later ideas":

- A single policy file compiled to Cedar at the gateway and to the admission policy, with a CI check that the layers agree, and a break-glass path with a ticket and two approvers.
- A signed, hash-chained audit ledger with write-once approval receipts, automatic detection of unexpected plan-record transitions (turning remediation off), and offline verification of an export.
- Revert of a rollback (a rollback to the previous template, by hash).
- Agent-facing evidence tool; weekly freeze windows; per-agent Cedar permits.
- Moving the scheduled sentinel and sweeper to EventBridge Scheduler.

**Done when (milestone):** a degraded change is detected and, if approved with revert, reverted; a rollback to a named revision applies exactly that template; a freeze refuses changes and the cluster refuses a write outside the bounds; and each change can be exported with its approval chain from a record the executor cannot alter.

## Milestone 3: Quick wins and guard rails (implemented and tested on a test cluster)

**Goal:** fix what users hit first (Helm releases refused as GitOps, a plan TTL too short to approve in chat, slow end-to-end runs, no release process) and put tests around the agent guidance before it is shortened.

- GitOps ownership by controller markers: the Argo CD tracking-id annotation and the Flux kustomize and helm labels; `app.kubernetes.io/instance` alone no longer counts, and `gitops_markers` restores it for Argo CD label tracking ([Guards](guards.md#gitops-ownership)).
- Plan TTL by approval mode (900 s in `client`, 300 s otherwise), capped at 3600 s.
- The plan rate limit removed; `plan_rate_limited` stays in the contract for one release, never returned.
- A test profile (`terraform/test.tfvars.example`), `e2e_gateway.py --reset-counters` for test deployments, and test values read from `terraform output`.
- Guidance sync tests: the `AGENTS.md` tables against the tool schemas and error codes, tool and argument names in the skills, contract links, word-count ceilings.
- [CHANGELOG.md](../CHANGELOG.md) and [Releasing](releasing.md) (semver, supported versions, one tool schema batch per release).
- A CloudWatch dashboard and a shadow-mode recipe ([deploy](deploy.md#shadow-mode)); approval mode guidance ([deploy](deploy.md#approval-mode)).

**Done when:** a Helm-only Deployment is not GitOps-managed and a Flux one is refused; a client-mode plan's `expires_at - created_at` is 900 on a test deployment; the e2e budget cases pass without waiting for the clock hour; the sync tests run in CI.

## Milestone 4: Simplify, then make upgrades one command

**Goal:** remove the optional modes and turn the multi-step manual upgrade into one resumable command.

- Remove legacy mode (`caller_interceptor_enabled = false`) and the `verify_enabled` switch, with `moved` blocks so existing deployments only see moves.
- `make upgrade` (`scripts/upgrade.py`): preflight on the kill switch, a `terraform plan -json` safety check against deletes of the gateway, Cognito, the plan table or the audit bucket, known two-step migrations, `enable-write` only when the tool set changed, a sentinel run, `install-guidance` when the guidance changed, and the smoke test; resumable.

**Done when:** on the test deployment `terraform plan` shows only moves, and `make upgrade` runs end to end.

## Milestone 5: CI tier 1 with kind and moto

**Goal:** catch guard regressions on every pull request instead of only on a shared cluster.

- An integration harness on kind with impersonated reader and writer groups and moto for DynamoDB, SSM and S3, calling the Lambda handlers in process; most end-to-end scenarios move there. Live runs keep only Cognito, the gateway interceptor, the DevOps Agent association, EKS access entries and VPC networking.

**Done when:** the integration job is green on every pull request in under 10 minutes.

## Milestone 6: Several namespaces, with policy and approvers per namespace

**Goal:** point kube-handrail at real service namespaces.

- `var.namespaces`: per namespace replica bounds, allowed actions, optional approvers and GitOps markers, packaged with the Lambdas (not SSM); empty keeps today's single sandbox namespace.
- Approvers per namespace (optional; where set, only they may approve there).
- RBAC and one admission policy rendered from the same policy, and a plan-time check that the cluster's admission policy matches it (a narrow ClusterRole reads the policy and its binding).

**Done when:** in a two-namespace test, an action allowed in one namespace is refused in the other, the wrong namespace's approver is refused, and a binding switched to `Warn` makes plans `conflict`.

## Milestone 7: Shorter docs and agent context

**Goal:** about 30,000 words of documentation instead of 52,000, and about 2,600 words of agent context instead of 4,700.

- `mcp/CONTRACT.md` as a reference of at most 6,500 words, with appendices; `AGENTS.md` at most 1,200 words with every "Never" rule kept; each skill at most 800 words; a quickstart.
- The word-count ceilings of the Milestone 3 sync tests lowered to these targets.

**Done when:** the sync tests pass with the new ceilings, and a before/after live chat comparison shows no change in behaviour.

## Milestone 8: Tool schema release #1 (HPA, health checks, change reference)

**Goal:** act on real workload capacity and verify changes by application health, in one schema release: one `enable-write`, one `install-guidance`.

- `plan_adjust_hpa` / `apply_adjust_hpa`, with its own record fields, blast radius on the resulting pod change, verification and automatic revert.
- `health_check` v1, CloudWatch only: named checks the operator defines, pinned in the plan, with `degraded` and revert on a regression. SLO and burn-rate checks, planned earlier as Prometheus SLO tools, are checks in this catalog.
- `change_ref`, an untrusted display-only reference; `both` mode gets a live chat test.

**Done when:** on kind, HPA changes verify and revert; a regressed custom metric reverts a scale-down; the upgrade finishes with one `enable-write`.

## Milestone 9: Approver and executor identity, audit quality

**Goal:** answer "who approved, who applied" honestly.

- The chat approval in `client` mode cannot be bound to a plan (see Open questions), so the audit-grade approver is the server approval in `server` and `both` mode; this is documented, and no CloudTrail correlation is built.
- `approval_mode` on the plan record; `scripts/audit.py verify` flags records consumed without an approver, on the wrong channel or rewritten by the executor; a User-Agent with plan and correlation ID for matching against the EKS audit log.

**Done when:** `audit.py verify` reports nothing after an end-to-end run.

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
- A signed, hash-chained audit ledger with write-once approval receipts, automatic detection of unexpected plan-record transitions, and offline verification of an export ([ADR 0007](adr/0007-durable-audit-record.md), alternative A).
- A single policy file compiled to Cedar at the gateway and to the admission policy, with a CI check that the layers agree, and a break-glass path with a ticket and two approvers.
- Prometheus and Amazon Managed Service for Prometheus as health check backends (after the CloudWatch catalog of Milestone 8 proves its value).
- An Alertmanager-to-DevOps Agent webhook trigger with deduplication, signing, throttling and a sanitized snapshot. ([Webhook docs](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-invoking-devops-agent-through-webhook.html))
- Node triage (unhealthy nodes, conditions, taints, pressure, failing pods) and change correlation (rollouts, image changes and GitOps history in one timeline).
- StatefulSets; a stdio proxy and a second client guide; a nightly end-to-end run on real AWS; prebuilt Lambda packages; a fleet (multi-cluster) guide.

## Open questions

These are unverified and may change the design.

- **Gateway authentication from DevOps Agent.** OAuth client credentials works (Milestone 0). Whether DevOps Agent's SigV4 signing works against an AgentCore Gateway with IAM inbound authorization is untested. ([Connecting MCP servers](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-mcp-servers.html))
- **Persistence of tool classification.** Whether `MUTATIVE` / `READ_ONLY` classifications survive infrastructure-as-code updates of the MCP server association, given that unclassified tools default to `READ_ONLY`. Partly answered: a Terraform update of a gateway target (a changed tool description) left the association's classifications unchanged; an update of the association itself replaces its whole configuration, which is why `enable-write` writes every tool explicitly and the drift sentinel watches it. ([Directed actions](https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-working-with-directed-actions.html))
- **Conditional Cedar policies and tool listing.** How argument-conditioned `permit` and `forbid` policies affect which tools appear in `tools/list`. ([Action-scoped policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-action-resource.html))
- **DevOps Agent tool limits.** The per-call timeout and response size limit for custom MCP tools, which determine output and time budgets.
- **DevOps Agent approval reference.** Whether the approval reference that DevOps Agent requires for `MUTATIVE` calls reaches the gateway. If it does, it could be recorded as evidence; it would not be used as a gate ([ADR 0002](adr/0002-server-verified-approval.md)).
- **CloudTrail data events as executor identity.** Whether CloudTrail DynamoDB data events on the plan table can show the principal that consumed a plan, so that the executor identity of a change is observed rather than attributed by construction (Milestone 2, item 6).

Answered by the identity spike for Milestone 9 (S0): CloudTrail records a DevOps Agent chat approval (`UpdateApprovalAction`) as a management event with the approver's IAM identity, but redacts every tool argument, the plan ID included, and the request carries no approval ID. A `client`-mode approval can therefore be matched to a plan only by time, not bound to it; the audit-grade approver is the server approval (`server` or `both`), and `client` is an evaluation and convenience mode ([deploy](deploy.md#approval-mode)).

Answered by Milestone 3: Flux-managed objects carry the kustomize-controller labels `kustomize.toolkit.fluxcd.io/name` and `/namespace`, or the helm-controller labels `helm.toolkit.fluxcd.io/name` and `/namespace`; they are refused like Argo CD-managed ones (implemented from the Flux documentation and tested on a test cluster with the labels set by hand, not with a running Flux).

Answered by Milestone 1, item 6: a gateway REQUEST interceptor can pass verified caller claims (the OAuth `client_id`) to the tool Lambdas, and a client cannot spoof them. A gateway has one REQUEST and one RESPONSE interceptor at most ([Interceptor types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html)).

Answered by Milestone 0: how DevOps Agent treats tool classification. A `MUTATIVE` tool requires a chat approval pinned to the exact arguments and is cancelled outside chat; a write tool classified `READ_ONLY` runs without approval, within the server-side limits.
