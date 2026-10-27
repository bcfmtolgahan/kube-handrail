# ADR 0001: Scope - a guarded remediation layer, not an AI SRE agent

- Status: Proposed
- Date: 2026-10-27

## Context

A review of the current platform landscape for AI agents operating on Amazon EKS found:

- **Investigation and RCA are well covered.** AWS DevOps Agent is generally available and offers investigations, CloudWatch integration, skills, custom MCP servers and chat integrations.
  ([GA announcement](https://aws.amazon.com/blogs/mt/announcing-general-availability-of-aws-devops-agent/), [supported Regions](https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-supported-regions.html))
  In open source, HolmesGPT covers the same ground. ([HolmesGPT](https://github.com/HolmesGPT/holmesgpt))
- **DevOps Agent's native EKS access is read-only.** "The agent cannot create, modify, or delete any resources in your cluster."
  ([EKS access setup](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-aws-eks-access-setup.html))
- **DevOps Agent can call custom MCP tools that write**, if they are classified `MUTATIVE`, directed actions are enabled, and an operator approves each call in chat. Unclassified tools default to `READ_ONLY` and run without approval.
  ([Directed actions](https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-working-with-directed-actions.html))
- **AWS guidance recommends read-only custom MCP servers.** The security and MCP connection pages recommend allowlisting only read-only MCP tools with read-only credentials. Exposing guarded write tools through directed actions knowingly departs from that recommendation.
  ([DevOps Agent security](https://docs.aws.amazon.com/devopsagent/latest/userguide/aws-devops-agent-security.html), [Connecting MCP servers](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-mcp-servers.html))
- **Existing Kubernetes MCP servers with write capability offer coarse controls.** As of their documentation at time of writing:
  - The awslabs EKS MCP server documents two global flags, `--allow-write` and `--allow-sensitive-data-access`, and no namespace, per-tool or dry-run controls. ([awslabs eks-mcp-server](https://awslabs.github.io/mcp/servers/eks-mcp-server))
  - The hosted Amazon EKS MCP server is in preview. Its write tools are `manage_k8s_resource` (create, replace, patch or delete in one tool), `apply_yaml`, `manage_eks_stacks` and `add_inline_policy`; write operations work only against clusters with a public endpoint. No namespace scoping or dry run is documented. ([EKS MCP tools](https://docs.aws.amazon.com/eks/latest/userguide/eks-mcp-tools.html))
  - Because DevOps Agent classifies per tool, a tool that combines patch and delete cannot be classified `MUTATIVE` for one operation and `DESTRUCTIVE` for the other.

  Both servers evolve quickly and must be re-checked.
- **Self-hosted Prometheus is not covered by the AWS-maintained MCP server.** The awslabs Prometheus MCP server targets Amazon Managed Service for Prometheus, as of its README at time of writing.
  ([prometheus-mcp-server README](https://raw.githubusercontent.com/awslabs/mcp/main/src/prometheus-mcp-server/README.md))

## Decision

Build a **complement layer**, not a competitor:

1. Provide guarded, policy-checked, auditable **write** tools for EKS (scale, restart, rollback of Deployments) using a plan/apply pattern, for any MCP client, with AWS DevOps Agent as the primary integration target.
2. Provide a small set of **read** tools only where they serve that flow or fill a documented gap: plan tools, workload status, node triage, change correlation, and SLO / alert tools for self-hosted Prometheus.
3. Provide an **Alertmanager to DevOps Agent** trigger so investigations start from existing alerts.
4. Leave investigation, RCA, CloudWatch, chat and generic Kubernetes reads to the client and to existing MCP servers.
5. Keep an agent runtime out of the core project. A thin example client may live under `examples/` later.

## Alternatives considered

### A. Build a self-hosted AI SRE agent

Own agent runtime, own prompts, own investigation loop.

Rejected. DevOps Agent covers this in managed form and HolmesGPT covers it in open source. Effort would go into re-implementing commodity capabilities instead of the unaddressed part (safe writes).

### B. Contribute to HolmesGPT instead

HolmesGPT is open source (Apache 2.0). Its README lists a Kubernetes Remediation toolset (scaling, rollbacks and resource edits via MCP), which overlaps the action set planned here, while also describing HolmesGPT as read-only by design. ([HolmesGPT](https://github.com/HolmesGPT/holmesgpt))

Not chosen for now. The target here is specifically the AWS path (AgentCore Gateway, Cedar policy, DevOps Agent directed actions, EKS access entries), which is outside HolmesGPT's core. The guard design (single-use plans, `resourceVersion` preconditions, admission policy) could later be proposed upstream as a toolset. Kept as an open option, not a rejection.

### C. Use the hosted Amazon EKS MCP server or the awslabs eks-mcp-server in write mode

Register an existing EKS MCP server with write access and rely on client-side approval.

Rejected as the write path. Documented controls are global or absent (no namespace allowlist, argument limits, single-use plan or concurrency precondition), the hosted server writes only to public-endpoint clusters, and its mixed create/patch/delete tool cannot be classified precisely. Classification mistakes would expose unguarded writes. Registering the hosted EKS MCP server behind our gateway as an MCP target was also rejected, mainly because it would import unguarded write tools. Gateway MCP targets do support SigV4 outbound authentication, but only for targets behind services that natively verify SigV4; the hosted EKS MCP endpoint is not on that documented list and is untested ([Gateway MCP server targets](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-target-MCPservers.html)). These servers remain the recommended path for generic reads.

### D. Use the awslabs prometheus-mcp-server for SLO tools

Rejected for self-hosted Prometheus because it targets Amazon Managed Service for Prometheus. If a user runs AMP, that server is the better choice for raw queries; the SLO tools here may still add burn-rate and error-budget logic on top.

## Consequences

- The project's value depends on the write path being usable from DevOps Agent end to end. This is not yet demonstrated; Milestone 0 in the [roadmap](../roadmap.md) tests it first.
- Read and write tools must be separate tools on separate gateway targets and Lambdas, because DevOps Agent classifies per tool and Cedar argument conditions need per-action policies.
- Safety cannot rely on client classification alone. The server must reject writes without a valid plan, regardless of how the tool is classified.
- Several integration details are unverified (inbound SigV4 to the gateway, `toolDetails` drift after IaC updates, how input-conditional Cedar policies affect `tools/list`, interceptor ordering). They are listed as open questions in the [roadmap](../roadmap.md) and may change the design.
- The project inherits a dependency on recently released AWS features (AgentCore Policy, DevOps Agent directed actions) and on the hosted EKS MCP server, which is explicitly in preview. Breaking changes there are an accepted risk.
- Scope stays deliberately narrow: no CloudWatch tools, no generic Kubernetes reads, no delete-class actions, no autonomous writes.
