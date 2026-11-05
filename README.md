# kube-handrail

Guarded remediation layer for AI agents on Kubernetes: **plan → policy → approve → apply → audit**. MCP tools for Amazon EKS, intended to work with AWS DevOps Agent and other MCP clients.

> **Status: Milestones 0 and 1 are implemented and tested on test clusters.** That covers a workload status tool and one guarded plan/apply pair for scaling, server-verified approval, the oscillation breaker, the impact preview and the drift sentinel. Correlation IDs, tool output hygiene with a workload events tool, agent guidance, and deployment to private-endpoint clusters are tested as well. Restart (Milestone 2, item 1) is implemented and tested on a test cluster. Rollback by exact revision (item 2) is implemented and tested on a test cluster. Per-agent identity, budgets and kill switch (item 3) are implemented and tested on a test cluster. The Cedar and admission policy layers and the later milestones are planned, not built. See [Status](#status).

## Why

AI agents are already good at *investigating* Kubernetes problems. Letting them *write* to a cluster safely is the gap.

- **AWS DevOps Agent's native EKS access is read-only.** The setup guide states the agent "cannot create, modify, or delete any resources in your cluster."
  ([EKS access setup](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-aws-eks-access-setup.html))
- **Custom MCP tools can mutate, but only through directed actions.** Tools classified `MUTATIVE` require directed actions to be enabled and per-action operator approval in chat. A mutating call made outside chat (for example during an autonomous investigation) fails instead of asking for approval. Tools classified `DESTRUCTIVE` are never invoked.
  ([Directed actions](https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-working-with-directed-actions.html))
- **Unclassified tools default to `READ_ONLY`.** If an MCP server association is registered or updated programmatically without `toolDetails`, every tool is treated as `READ_ONLY` and runs without approval. A single missed classification turns a write tool into an unapproved write tool. (Same source.)
- **Tool output is an injection surface.** AWS's own security guidance asks operators to ensure connected data sources are trusted, and recommends that custom MCP servers be permitted only read-only actions. Exposing write tools, even behind directed actions, knowingly departs from that recommendation; the server-side controls below are meant to compensate, not to make the risk disappear.
  ([DevOps Agent security](https://docs.aws.amazon.com/devopsagent/latest/userguide/aws-devops-agent-security.html), [Connecting MCP servers](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-mcp-servers.html))

The human approval step in the client is necessary but not sufficient. kube-handrail is meant to enforce limits on the server side so that a misclassified tool, a misconfigured client, or a prompt-injected agent still cannot make an unbounded change.

Whether a `MUTATIVE` custom MCP tool is a workable path for in-cluster writes from DevOps Agent end to end is itself an assumption. Milestone 0 ([results](docs/milestone-0.md)) tested it on a test cluster; see the [roadmap](docs/roadmap.md) for what remains unverified.

## How it is intended to work

This is the target design. Implemented today: the `k8sr` and `k8sw` targets, the plan/apply flow with single-use plans, the namespace, replica, GitOps and HPA checks, the blast-radius budget, the measured impact preview, the kill switch, namespace-scoped RBAC, client-side approval in DevOps Agent, [server-side approval](mcp/CONTRACT.md#server-side-approval) (`request_approval`, an IAM-authenticated approval API and CLI, and an apply that consumes only an approved plan), correlation IDs with a structured record and metrics per tool call, and [untrusted output](mcp/CONTRACT.md#untrusted-output) handling for cluster-sourced text, used by `get_workload_status`, `get_workload_events` and `get_rollout_history`. The actions are scale, restart and [rollback by exact revision](mcp/CONTRACT.md#rollback). The caller identity interceptor names the requesting agent on every plan, with per-agent budgets and kill switches ([Agent identity](mcp/CONTRACT.md#agent-identity)). Everything else shown below is planned.

```
  MCP client
  (AWS DevOps Agent, Kiro, other MCP clients)
        │  MCP (streamable HTTP), OAuth client credentials
        │  (SigV4 to be validated)
        ▼
┌────────────────────────────────────────────────────────────┐
│ Amazon Bedrock AgentCore Gateway                           │
│   AgentCore Policy (Cedar, default deny, argument limits)  │
│   Interceptors: caller identity, output sanitization       │
├───────────────────────────┬──────────────┬─────────────────┤
│ k8sr (READ_ONLY)          │ k8sw         │ prom (READ_ONLY)│
│ workload status, events,  │ (MUTATIVE)   │ SLO / burn rate,│
│ plan_*, node triage,      │ apply_*      │ golden signals, │
│ changes                   │              │ firing alerts   │
└────────────┬──────────────┴──────┬───────┴────────┬────────┘
             ▼                     ▼                ▼
      read/plan Lambda       write Lambda       Prometheus Lambda
      (reader role)           + guard.py         (self-hosted Prometheus,
             │               (writer role)        Alertmanager)
             │                     │
             │     ┌───────────────┴──────────┐
             │     │ DynamoDB: plans, audit,  │
             │     │ counters                 │
             │     └──────────────────────────┘
             ▼                     ▼
      ┌─────────────────────────────────────────┐
      │ Amazon EKS API server                   │
      │   RBAC (namespace-scoped RoleBindings)  │
      │   ValidatingAdmissionPolicy             │
      └─────────────────────────────────────────┘

  Alertmanager ──webhook──► trigger Lambda ──HMAC webhook──► DevOps Agent
                            (dedupe, sanitized snapshot to S3)   [planned]
```

A remediation follows this sequence:

1. **Plan.** The agent calls a read-only tool such as `plan_scale_deployment`. It reads current state, runs preflight checks (HPA and GitOps ownership, the blast-radius budget and the [oscillation breaker](docs/guards.md), and for a rollback the one ReplicaSet the revision resolves to), computes the diff locally, previews the measured impact for the approver (pods added or removed, PodDisruptionBudget headroom, ready and projected endpoints of the Services in front of it, and warnings such as a Service left without endpoints), and stores a plan with `plan_id`, `args_hash`, the object's `uid`, `generation` and current replica count, and a short expiry.
2. **Policy.** The matching `apply_*` call passes through Cedar at the gateway, which checks tool arguments (namespace allowlist, replica bounds). Planned; today the same limits are enforced only by the write Lambda.
3. **Approve.** In `client` mode the client shows the exact arguments to a human; in DevOps Agent this is the chat approval for a `MUTATIVE` tool. In `server` mode (the default) the agent calls the `READ_ONLY` tool `request_approval`, approvers are notified with the plan as the server recorded it, and a listed human approves through an IAM-authenticated API outside MCP (`scripts/approve.py`); the approval is pinned to the plan's arguments and also triggers the apply.
4. **Apply.** The write Lambda's guard re-validates everything: plan exists, is unexpired and unused (single-use, conditional write), arguments match, in `server` mode an unexpired approval by someone other than the requester covers exactly these arguments (in the same conditional write), the object is the same one with an unchanged spec and no GitOps marker, kill switch is on, the change is within the blast-radius budget, and the [oscillation breaker](docs/guards.md) (manual-change override, per-object cooldown, flapping, per-object and global hourly apply budgets) still allows the change. It then runs a server-side dry run and the real patch, with a `resourceVersion` precondition so nothing can change in between.
5. **Audit.** The result is written to the plan record in DynamoDB with the apply's correlation ID (the gateway's request ID, or the approver's for an apply it triggered), which is also set as the `kube-handrail.io/correlation-id` annotation on the target object. The plan, approval request, decision and apply each record their own correlation ID, and every tool call logs one structured record that is also a CloudWatch metric ([Observability](mcp/CONTRACT.md#observability)). Joining with the gateway's own logs and Kubernetes audit logs is not set up here.

## Design principles

- **Plan/apply split.** Every write is two tools: a `READ_ONLY` `plan_*` and a `MUTATIVE` `apply_*` with identical arguments, so policy and the human approver see real values.
- **Single-use, short-lived plans.** A plan can be applied once, within minutes, and only to the object it was computed from (same `uid`) with its spec unchanged (same `generation` and replica count). Concurrent spec changes return a conflict; status churn during a rollout does not.
- **Misclassification loses the approval, not the limits.** If the write tool is accidentally left `READ_ONLY`, DevOps Agent can plan and apply on its own without asking anyone (observed in testing). The server-side limits still hold: allowed namespaces, replica bounds, the blast-radius budget, GitOps and HPA checks, single-use plans and the kill switch. Classification is therefore checked after every registration change, and a scheduled [drift sentinel](mcp/CONTRACT.md#drift-sentinel) turns the kill switch off and alerts when a write tool is no longer `MUTATIVE` or the tool set changes. In `server` mode a misclassified write tool still cannot apply anything without a server approval.
- **Defense in depth (target design; Cedar and admission policy are planned).** Each layer assumes the others may fail:
  - `guard.py` in the write Lambda (authoritative check)
  - Cedar policy at the gateway ([AgentCore Policy examples](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-action-resource.html))
  - ValidatingAdmissionPolicy in the cluster ([Kubernetes docs](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/))
  - Kubernetes RBAC with separate reader and writer identities, writer bound only to allowlisted namespaces
  - Server-verified approval, pinned to the plan's arguments (`server` and `both` mode)
  - Client-side approval (DevOps Agent directed actions)
- **GitOps-aware.** Resources managed by Argo CD (and Flux, once its ownership markers are validated) are not modified; the plan returns the error `gitops_managed`. ([Argo CD resource tracking](https://argo-cd.readthedocs.io/en/stable/user-guide/resource_tracking/))
- **No delete-class tools.** No delete, no generic `apply_yaml`. The actions are scale, restart and rollback by exact revision of Deployments. A rollback writes only the template of a ReplicaSet the Deployment controls, re-read and hash-checked at apply.
- **Tool output is untrusted.** In the tool Lambdas (the gateway interceptor in the diagram is not used for this), text from the cluster that anyone with write access can set (condition and event reasons and messages, container images and change-causes today; logs and alert annotations once there are tools for them) is stripped of control and format characters, redacted for common secret patterns, cut to a field limit with an explicit marker, and wrapped in `<untrusted>...</untrusted>`; list responses have a size limit. The delimiters are a hint to the model, not a security boundary: the boundary is that read tools change nothing and every change needs a human approval pinned to its arguments. See [Untrusted output](mcp/CONTRACT.md#untrusted-output). ([OWASP LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/))
- **Bounded blast radius.** A kill switch in SSM Parameter Store, a maximum step per change (`MAX_REPLICA_STEP_PERCENT` of the current replica count, enforced whatever the approval says), a per-object cooldown, and per-object and global hourly apply budgets. Per agent, an hourly apply budget and a kill switch of its own.
- **One classification source.** Every tool's `READ_ONLY` / `MUTATIVE` classification is in `mcp/classification.json`; registration, the drift sentinel and a CI check all read it, and CI fails if a write tool is not `MUTATIVE`.

## Non-goals

- **Not an AI SRE agent.** Investigation and root-cause analysis are handled by the MCP client (for example AWS DevOps Agent). See [ADR 0001](docs/adr/0001-scope.md).
- **No generic Kubernetes read tools** (`get_pods`, `get_logs`, `describe`, ...) that duplicate the hosted Amazon EKS MCP server (in preview) or DevOps Agent's native EKS access. Only summarizing tools that serve the plan/apply flow, node triage and change correlation are in scope; `get_workload_events` returns only the events of one Deployment and the objects it owns, for deciding on a plan. ([EKS MCP tools](https://docs.aws.amazon.com/eks/latest/userguide/eks-mcp-tools.html))
- **No CloudWatch tools.** DevOps Agent includes CloudWatch; other clients can use existing CloudWatch MCP servers.
- **No unapproved remediation.** Every write is covered by a human approval pinned to its exact arguments and verified by the server ([ADR 0002](docs/adr/0002-server-verified-approval.md)). In `client` mode the approval is given in the client (DevOps Agent directed actions); in `server` and `both` mode the server requires its own approval record.

## Status

Milestone 0 is implemented and tested on a test cluster: `get_workload_status` and a guarded `plan_scale_deployment` / `apply_scale_deployment` pair behind an AgentCore Gateway, with Terraform, namespace-scoped RBAC, end-to-end guard tests and AWS DevOps Agent registration. Setup, tests and observed behavior are in [Milestone 0](docs/milestone-0.md). There is no release.

Milestone 1 (server-verified write path): items 1-5 are implemented and tested on a test cluster, and the caller identity spike (item 6) is done, with an interceptor that was experimental and off by default then (Milestone 2, item 3 turns it on). Tool output hygiene, with the `get_workload_events` tool, and agent guidance ([`AGENTS.md`](AGENTS.md) and a DevOps Agent skill, installed by `scripts/devops_agent_register.py install-guidance`) are implemented and tested on a test cluster, as are correlation IDs and tool metrics. Deployment to a new account, including clusters with a private endpoint ([Deploying to a new account](docs/deploy.md)), is implemented and tested, including a fresh deployment against a cluster with only a private endpoint, reaching AWS through VPC endpoints. With that, Milestone 1 is complete; the next milestones are in the [roadmap](docs/roadmap.md). Integration details that are still unverified are listed there under "Open questions".

Milestone 2, item 1 (action framework and restart): `plan_restart_deployment` / `apply_restart_deployment` and the generic plan/apply path are implemented and tested on a test cluster, in `client` and `server` mode and through a DevOps Agent chat. See [Restart](mcp/CONTRACT.md#restart).

Milestone 2, item 2 (rollback by exact revision): `get_rollout_history` and `plan_rollback_deployment` / `apply_rollback_deployment` are implemented and tested on a test cluster, in `client` and `server` mode and through a DevOps Agent chat. See [Rollback](mcp/CONTRACT.md#rollback).

Milestone 2, item 3 (per-agent identity, budgets and kill switch): the caller interceptor is on by default, plans name the requesting agent, only that agent can request approval for its plan or apply it through the gateway, each agent has an hourly apply budget and a kill switch, and the drift sentinel checks the gateway's interceptor and allowed clients. Implemented and tested on a test cluster, in `client` and `server` mode and with DevOps Agent. See [Agent identity](mcp/CONTRACT.md#agent-identity).

## Planned layout

Subject to change as the design is validated.

```
mcp/
  common/        guard, sanitize, envelope, EKS auth
  k8s_read/      read and plan tools (k8sr)
  k8s_write/     apply tools (k8sw)
  prom/          SLO and alert tools (prom)
  trigger/       Alertmanager -> DevOps Agent webhook
  */tools.json   tool schemas, single source for Terraform and tests
k8s/
  rbac/          reader and writer roles
  vap/           ValidatingAdmissionPolicies
policies/        Cedar policies
terraform/       gateway, targets, Lambdas, DynamoDB, IAM, DevOps Agent association
scripts/         tool classification (toolDetails) and drift checks
agent/skills/    DevOps Agent skills
tests/
docs/
AGENTS.md        tool contract for MCP clients
```

## Documentation

- [AGENTS.md](AGENTS.md) - instructions for agents and MCP clients: the tool flow, what to do on each error, untrusted tool output
- [ADR 0001: Scope](docs/adr/0001-scope.md) - why a complement layer and not another AI SRE agent
- [ADR 0002: Server-verified approval](docs/adr/0002-server-verified-approval.md) - approvals pinned to exact arguments and verified by the server
- [ADR 0003: Agent identity per OAuth client](docs/adr/0003-agent-identity.md) - which agent asked for each change, and per-agent limits
- [Milestone 0](docs/milestone-0.md) - deploying and testing the vertical slice
- [Deploying to a new account](docs/deploy.md) - prerequisites, names, RBAC, networking for clusters with a private endpoint, and the order of steps
- [Guards](docs/guards.md) - blast-radius budget, impact preview, what a rollback trusts, the oscillation breaker (flapping, cooldown, per-object and global budgets, plan rate limit and manual-change checks), and the per-agent budget and kill switch
- [Drift sentinel](mcp/CONTRACT.md#drift-sentinel) - what it checks, outcomes, and how to restore remediation after drift
- [Server-side approval](mcp/CONTRACT.md#server-side-approval) - approval modes, `request_approval`, the approval API and who can approve
- [Observability](mcp/CONTRACT.md#observability) - correlation IDs, the per-call tool record and its metrics, and the internal error alarm
- [Untrusted output](mcp/CONTRACT.md#untrusted-output) - how text from the cluster is cleaned and wrapped before an agent sees it, and why that is a hint rather than a boundary
- [Roadmap](docs/roadmap.md) - milestones and open questions

## Contributing

The project is at an early stage. Issues and discussion are welcome, and feedback on the design (safety model, policy structure, scope) is especially useful.

## License

Apache License 2.0. See [LICENSE](LICENSE).
