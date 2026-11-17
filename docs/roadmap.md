# Roadmap

What comes next. Milestones 0 to 6, 8 and 9 are done; what they built and how they were tested is in [Milestones done](history/milestones.md). The order below may change as the design is validated.

## Milestone 7: Shorter docs and agent context

**Goal:** documentation that is short enough to read, and agent context that costs less.

- `mcp/CONTRACT.md` at most 6,500 words, with the details in appendices under `mcp/contract/`; `AGENTS.md` at most 1,200 words with every "Never" rule kept; each skill at most 800 words; a [quickstart](quickstart.md); test and status logs under `docs/history/`.
- The word-count ceilings of the sync tests lowered to the new sizes.

**Done when:** the sync tests pass with the new ceilings (done); a before/after live comparison with `scripts/devops_agent_chat.py`, each scenario in a new chat (approved scale, rejected approval, `manual_change`, `change_frozen`, an instruction injected in an event message, a rollback with an image diff), shows the same behaviour; and the quickstart has been timed with a new prefix. The last two are still to do.

## Later ideas

Not committed; listed for discussion.

- **GitOps pull-request path.** For GitOps-managed objects, open a minimal pull request that passes the same bounds and policy checks, instead of returning `gitops_managed`. Merge is the approval, sync is the apply. Argo CD first, Flux later.
- **Standing approvals (off by default).** Pre-approved, expiring, budgeted grants for a narrow, repeated change, requiring the oscillation breaker and verify-and-revert. This would change a central non-goal and needs its own ADR.
- **Named health checks in the verification** (deferred from Milestone 8). Operator-defined CloudWatch checks, pinned in the plan, that make a change `degraded` on a regression; Prometheus later. Deferred because observability is DevOps Agent's job, and the catalog, the `monitoring` VPC endpoint, a new error code and windows of at least 300 seconds cost more than they add now.
- A single policy file compiled to Cedar at the gateway and to the admission policy, with a CI check that the layers agree, and a break-glass path with a ticket and two approvers.
- A signed, hash-chained audit ledger with write-once approval receipts, automatic detection of unexpected plan-record transitions, and offline verification of an export ([ADR 0007](adr/0007-durable-audit-record.md), alternative A).
- Revert of a rollback (a rollback to the previous template, by hash).
- An agent-facing evidence tool, weekly freeze windows and per-agent Cedar permits.
- Moving the scheduled sentinel and verifier to EventBridge Scheduler.
- An Alertmanager-to-DevOps Agent webhook trigger with deduplication, signing, throttling and a sanitized snapshot ([webhook docs](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-invoking-devops-agent-through-webhook.html)).
- Node triage (unhealthy nodes, conditions, taints, pressure, failing pods) and change correlation (rollouts, image changes and GitOps history in one timeline).
- StatefulSets; a stdio proxy and a second client guide; a nightly end-to-end run on real AWS; prebuilt Lambda packages; a fleet (multi-cluster) guide with a cluster allowlist.
- An example thin client and a scenario and eval suite with fault injection; a published cost breakdown.

## Open questions

These are unverified and may change the design. Answered ones are in [Milestones done](history/milestones.md#answered-questions).

- **Gateway authentication from DevOps Agent.** OAuth client credentials works. Whether DevOps Agent's SigV4 signing works against a gateway with IAM inbound authorization is untested ([Connecting MCP servers](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-mcp-servers.html)).
- **Conditional Cedar policies and tool listing.** How argument-conditioned `permit` and `forbid` policies affect which tools appear in `tools/list` ([Action-scoped policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-action-resource.html)).
- **DevOps Agent tool limits.** The per-call timeout and response size limit for custom MCP tools, which set the output and time budgets.
- **CloudTrail data events as executor identity.** Whether DynamoDB data events on the plan table can show the principal that consumed a plan, so that the executor of a change is observed rather than attributed by construction.
