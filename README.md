# kube-handrail

Guarded remediation layer for AI agents on Kubernetes: **plan → policy → approve → apply → verify → audit**. MCP tools for Amazon EKS, built for AWS DevOps Agent and usable from other MCP clients.

> **Status:** 0.1.0. Scale, restart, rollback by exact revision and HPA bounds, each through a single-use plan; server-verified approval; the oscillation breaker, blast-radius budget and impact preview; per-agent identity, budgets and kill switches; verification after every apply with an optional pre-approved revert; change freezes; a cluster admission policy for the bounds; a write-once audit record; and the drift sentinel. Cedar at the gateway, break-glass and a signed audit ledger are on the roadmap. Details: [Milestones done](docs/history/milestones.md), [roadmap](docs/roadmap.md).

## Why

AI agents are good at *investigating* Kubernetes problems. Letting them *write* to a cluster safely is the gap.

- **DevOps Agent's native EKS access is read-only.** It "cannot create, modify, or delete any resources in your cluster" ([EKS access setup](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-aws-eks-access-setup.html)).
- **Custom MCP tools can write, but only through directed actions.** A `MUTATIVE` tool needs per-action operator approval in chat and fails outside chat; a `DESTRUCTIVE` tool is never invoked ([Directed actions](https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-working-with-directed-actions.html)).
- **Unclassified tools default to `READ_ONLY`.** One missed classification turns a write tool into an unapproved write tool.
- **Tool output is an injection surface.** AWS recommends that custom MCP servers be read-only ([DevOps Agent security](https://docs.aws.amazon.com/devopsagent/latest/userguide/aws-devops-agent-security.html)). Write tools depart from that knowingly; the server-side controls below compensate, they do not make the risk disappear.

The approval in the client is necessary but not enough. kube-handrail enforces limits on the server, so that a misclassified tool, a misconfigured client or a prompt-injected agent still cannot make an unbounded change.

## How it works

```
MCP client (AWS DevOps Agent, ...)
   │  MCP over HTTPS, OAuth client credentials
   ▼
AgentCore Gateway ── request interceptor (signs the calling agent)
   ├── k8sr (READ_ONLY): status, events, history, plan_*, request_approval, get_plan_outcome
   └── k8sw (MUTATIVE):  apply_*
          │ guard: plan, approval, bounds, breaker, budgets, kill switches
          ▼
   Amazon EKS API ── RBAC (namespace-scoped) ── ValidatingAdmissionPolicy (bounds)

DynamoDB plan table ──stream──► audit Lambda ──► S3 with Object Lock
Approver (IAM-signed URL) · Verifier (every minute) · Drift sentinel (every 5 minutes)
```

1. **Plan.** The agent calls a read-only `plan_*` tool. The server checks namespace, bounds, GitOps and HPA ownership, the blast radius and the breaker, measures the impact (pods, PodDisruptionBudget headroom, Service endpoints) and stores a single-use plan pinned to the object's `uid` and spec.
2. **Approve.** In `server` mode the agent calls `request_approval`; a listed human approves through an IAM-authenticated API outside MCP (`scripts/approve.py`), pinned to the plan's exact arguments, and the approval runs the apply. In `client` mode the client's own approval (the DevOps Agent chat) is the only one.
3. **Apply.** The write Lambda re-checks everything: the plan is unexpired and unused, the arguments match, the approval is valid and by someone other than the requester, the object is unchanged, the kill switches are on, and the breaker and budgets still allow it. Then a server-side dry run and a patch with a `resourceVersion` precondition.
4. **Verify.** After the plan's window the server checks whether the change reached its target: `healthy`, `degraded` or `unknown`. A change approved with `revert_on_failure` that is `degraded` is set back once.
5. **Audit.** Every step records a correlation ID in the plan record, on the object and in the logs, and every change to a plan record is written once to a bucket the executor cannot alter.

## Safety model

- **Plan/apply split.** Every write is a `READ_ONLY` `plan_*` and a `MUTATIVE` `apply_*` with identical arguments, so the approver sees real values.
- **Single-use, short-lived plans**, bound to the object and spec they were computed from.
- **Misclassification loses the approval, not the limits.** A write tool left `READ_ONLY` would run without a chat approval, but the server limits still hold, and in `server` mode nothing applies without a server approval. The drift sentinel turns the kill switch off when a write tool stops being `MUTATIVE`.
- **Defense in depth:** the guard in the write Lambda (authoritative), namespace-scoped RBAC, a ValidatingAdmissionPolicy for the bounds, server-verified approval, and the client's approval.
- **Bounded blast radius:** replica bounds per namespace, a maximum step per change, cooldown, flapping detection, per-object, per-agent and global hourly budgets, kill switches, change freezes.
- **GitOps-aware:** objects owned by Argo CD or Flux are refused; the change belongs in Git.
- **No delete-class tools**, no generic `apply_yaml`.
- **Tool output is untrusted:** cluster text is cleaned, redacted, cut and wrapped in `<untrusted>` before an agent sees it. That is a hint, not a boundary: the boundary is that reads change nothing and every change needs an approval pinned to its arguments.
- **One classification source**, `mcp/classification.json`, checked in CI.

Why each check exists: [Guards](docs/guards.md). The exact rules: [`mcp/CONTRACT.md`](mcp/CONTRACT.md).

**Which approval mode.** `server` is the default and gives an audit-grade record of who approved. With DevOps Agent in production, use `both`: the chat approval stays the agent's flow and the server approval is the record. `client` is for evaluation: the chat approval cannot be bound to a plan, because CloudTrail redacts the plan ID. For production, also set the audit bucket to `COMPLIANCE` ([Approval mode](docs/deploy.md#approval-mode), [Audit record retention](docs/deploy.md#audit-record-retention)).

## Non-goals

- **Not an AI SRE agent.** Investigation belongs to the MCP client ([ADR 0001](docs/adr/0001-scope.md)).
- **No generic Kubernetes or CloudWatch read tools** that duplicate the Amazon EKS MCP server or DevOps Agent; only summaries that serve the plan/apply flow.
- **No unapproved remediation.** Every write is covered by a human approval pinned to its exact arguments ([ADR 0002](docs/adr/0002-server-verified-approval.md)).

## Get started

1. [Quickstart](docs/quickstart.md): deploy, run the smoke test, connect DevOps Agent.
2. [Deploying to a new account](docs/deploy.md): prerequisites, names, namespaces, agents, approval mode, network, upgrades.
3. [Testing](docs/testing.md): the integration suite, the end-to-end guard tests and the DevOps Agent checks.

Supported versions (Kubernetes 1.30 or later for the admission policy, Terraform 1.5 or later) and the release rules are in [Releasing](docs/releasing.md).

## Documentation

- [AGENTS.md](AGENTS.md): instructions for agents: the flow, the rules, what to do on each error.
- [`mcp/CONTRACT.md`](mcp/CONTRACT.md) and its appendices: the binding definition of the tools, errors, plan record, guards, limits and configuration. For example [Drift sentinel](mcp/CONTRACT.md#drift-sentinel), [Approval API](mcp/contract/approval-api.md), [Untrusted output](mcp/contract/untrusted-output.md).
- [Guards](docs/guards.md): why each check exists.
- ADRs: [0001 Scope](docs/adr/0001-scope.md), [0002 Server-verified approval](docs/adr/0002-server-verified-approval.md), [0003 Agent identity](docs/adr/0003-agent-identity.md), [0004 Pre-approved revert](docs/adr/0004-pre-approved-revert.md), [0005 Change freezes](docs/adr/0005-change-freezes.md), [0006 Admission policy](docs/adr/0006-admission-policy.md), [0007 Durable audit record](docs/adr/0007-durable-audit-record.md).
- [Roadmap](docs/roadmap.md), [Milestones done](docs/history/milestones.md), [CHANGELOG.md](CHANGELOG.md), [Releasing](docs/releasing.md).

## Contributing

Issues and discussion are welcome, especially feedback on the safety model, the policy structure and the scope. Report vulnerabilities privately, as described in [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
