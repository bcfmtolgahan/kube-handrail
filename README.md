# kube-handrail

Guarded remediation layer for AI agents on Kubernetes: **plan → policy → approve → apply → audit**. MCP tools for Amazon EKS, intended to work with AWS DevOps Agent and other MCP clients.

> **Status: early design.** No code exists yet. This repository currently holds the design, scope decision, roadmap and open questions. Nothing described below is implemented or tested. See [Status](#status).

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

Whether a `MUTATIVE` custom MCP tool is a workable path for in-cluster writes from DevOps Agent end to end is itself an assumption. Milestone 0 of the [roadmap](docs/roadmap.md) exists to test it.

## How it is intended to work

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
│ workload status, plan_*,  │ (MUTATIVE)   │ SLO / burn rate,│
│ node triage, changes      │ apply_*      │ golden signals, │
│                           │              │ firing alerts   │
└────────────┬──────────────┴──────┬───────┴────────┬────────┘
             ▼                     ▼                ▼
      read/plan Lambda       write Lambda       Prometheus Lambda
      (sre-reader)           + guard.py         (self-hosted Prometheus,
             │               (sre-writer)        Alertmanager)
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
                            (dedupe, sanitized snapshot to S3)
```

A remediation follows this sequence:

1. **Plan.** The agent calls a read-only tool such as `plan_scale_deployment`. It reads current state, runs preflight checks (PDB, HPA, GitOps ownership, revision history), computes the diff locally, and stores a plan with `plan_id`, `args_hash`, `resourceVersion` and a short expiry.
2. **Policy.** The matching `apply_*` call passes through Cedar at the gateway, which checks tool arguments (namespace allowlist, replica bounds).
3. **Approve.** The client shows the exact arguments to a human. In DevOps Agent this is the chat approval for a `MUTATIVE` tool.
4. **Apply.** The write Lambda's guard re-validates everything: plan exists, is unexpired and unused (single-use, conditional write), arguments match, `resourceVersion` is unchanged, kill switch is on, cooldown and hourly budget are respected. It then runs a server-side dry run and the real patch.
5. **Audit.** The result is written to DynamoDB with a correlation ID that is also annotated on the target object and traceable through gateway and Kubernetes audit logs.

## Design principles

- **Plan/apply split.** Every write is two tools: a `READ_ONLY` `plan_*` and a `MUTATIVE` `apply_*` with identical arguments, so policy and the human approver see real values.
- **Single-use, short-lived plans.** A plan can be applied once, within minutes, against the exact `resourceVersion` it was computed from. Concurrent changes return a conflict.
- **Fail-safe without classification.** `apply_*` does nothing without a valid plan, so a tool that is accidentally left `READ_ONLY` still cannot write in one step.
- **Defense in depth.** Each layer assumes the others may fail:
  - `guard.py` in the write Lambda (authoritative check)
  - Cedar policy at the gateway ([AgentCore Policy examples](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-action-resource.html))
  - ValidatingAdmissionPolicy in the cluster ([Kubernetes docs](https://kubernetes.io/docs/reference/access-authn-authz/validating-admission-policy/))
  - Kubernetes RBAC with separate reader and writer identities, writer bound only to allowlisted namespaces
  - Client-side approval (DevOps Agent directed actions)
- **GitOps-aware.** Resources managed by Argo CD (and Flux, once its ownership markers are validated) are not modified; the plan returns `blocked: gitops_managed`. ([Argo CD resource tracking](https://argo-cd.readthedocs.io/en/stable/user-guide/resource_tracking/))
- **No delete-class tools.** No delete, no generic `apply_yaml`. The initial action set is scale, restart and rollback of Deployments.
- **Tool output is untrusted.** Logs, events and alert annotations are size-limited, stripped of control characters, redacted for secret patterns and wrapped in explicit untrusted-content delimiters. ([OWASP LLM01](https://genai.owasp.org/llmrisk/llm01-prompt-injection/))
- **Bounded blast radius.** Reserved concurrency on the write Lambda, a global hourly apply budget, and a kill switch in SSM Parameter Store.

## Non-goals

- **Not an AI SRE agent.** Investigation and root-cause analysis are handled by the MCP client (for example AWS DevOps Agent). See [ADR 0001](docs/adr/0001-scope.md).
- **No generic Kubernetes read tools** (`get_pods`, `get_logs`, `describe`, ...) that duplicate the hosted Amazon EKS MCP server (in preview) or DevOps Agent's native EKS access. Only summarizing tools that serve the plan/apply flow, node triage and change correlation are in scope. ([EKS MCP tools](https://docs.aws.amazon.com/eks/latest/userguide/eks-mcp-tools.html))
- **No CloudWatch tools.** DevOps Agent includes CloudWatch; other clients can use existing CloudWatch MCP servers.
- **No autonomous remediation.** Every write requires a human approval in the client.

## Status

Early design. There is no code, no Terraform, and no release. Several integration details are still to be validated and may change the design; they are listed under "Open questions" in the [roadmap](docs/roadmap.md). The first milestone is a vertical slice that proves one guarded write end to end.

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
skills/          DevOps Agent skills and runbook references
tests/
docs/
AGENTS.md        tool contract for MCP clients
```

## Documentation

- [ADR 0001: Scope](docs/adr/0001-scope.md) - why a complement layer and not another AI SRE agent
- [Roadmap](docs/roadmap.md) - milestones and open questions

## Contributing

The project is at the design stage. Issues and discussion are welcome, and feedback on the design (safety model, policy structure, scope) is especially useful.

## License

Apache License 2.0. See [LICENSE](LICENSE).
