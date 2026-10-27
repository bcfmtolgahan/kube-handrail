# Roadmap

Nothing below is implemented. This is the planned order of work; it will change as the design is validated.

## Milestone 0: Vertical slice

**Goal:** prove that one guarded write works end to end from AWS DevOps Agent before building anything else. If it does not, the scope in [ADR 0001](adr/0001-scope.md) needs revisiting.

- One read tool (`get_workload_status`) and a `plan_scale_deployment` / `apply_scale_deployment` pair.
- A minimal guard: the plan must exist, be unexpired and unused, and match the arguments and the object's `resourceVersion`; namespace allowlist and replica bounds.
- Separate reader and writer identities with namespace-scoped RBAC.
- Register the tools with DevOps Agent, classifying the write tool `MUTATIVE` in the same step it is added.

**Done when:**
- In chat, "scale web to 5" produces a plan, then an approval prompt showing the real arguments, then the apply.
- The apply fails during an autonomous investigation.
- A reused plan, an expired plan, or a changed object is rejected.
- Even if the write tool is wrongly classified `READ_ONLY`, an apply without a valid plan is rejected.

## Milestone 1: Safe read-only integration

**Goal:** a production-shaped, read-only deployment that DevOps Agent and other MCP clients can use.

- Read and write tools on separate gateway targets, Lambdas, IAM roles and EKS access entries; one tool per action, with schemas kept in a single source used by both infrastructure code and tests.
- Inbound authentication from DevOps Agent (OAuth client credentials by default) and tool classification managed as code, with a drift check.
- Tool output hygiene: size limits, control-character stripping, secret redaction, untrusted-content delimiters.
- Infrastructure as code that deploys cleanly to a new account, including clusters with a private endpoint.
- CI (formatting, linting, tests, policy validation) and basic self-observability with a correlation ID.
- DevOps Agent skills and an `AGENTS.md` describing the tool contract.

**Done when:** a fresh deployment returns real cluster data to DevOps Agent in chat, CI blocks a misclassified write tool, and a prompt-injection string in a pod log is returned wrapped and truncated without leading to a plan.

## Milestone 2: Guarded remediation

**Goal:** scale, restart and rollback of Deployments with every safety layer in place.

- Plan/apply for scale, restart and rollback, with preflight checks (PDB, HPA, revision history) and GitOps detection that blocks managed resources.
- Server-side dry run immediately before the real patch; kill switch, cooldown, concurrency limit and hourly apply budget.
- Cedar policy at the gateway (default deny, argument limits), rolled out in log-only mode before enforcement. ([Policy enforcement modes](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/policy-enforcement-modes.html))
- ValidatingAdmissionPolicy for the writer identity, rolled out in audit mode before deny.
- Audit trail traceable across gateway, Lambda, DynamoDB and the Kubernetes audit log.
- Approval rights and gateway configuration rights restricted to the appropriate roles.

**Done when:** a chat-approved change succeeds; autonomous, reused, expired, out-of-bounds or concurrently changed requests are rejected; and each rejection can be traced to the layer that made it.

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

- An example thin client and a scenario/eval suite with fault injection.
- Multi-cluster support with a cluster allowlist in policy.
- GitOps remediation via pull request instead of direct patches.
- Change-freeze windows enforced at the gateway.
- A published cost breakdown.

## Open questions

These are unverified and may change the design.

- **Gateway authentication from DevOps Agent.** Whether DevOps Agent's SigV4 signing works against an AgentCore Gateway with IAM inbound authorization, or whether OAuth client credentials is the only practical path. ([Connecting MCP servers](https://docs.aws.amazon.com/devopsagent/latest/userguide/configuring-integrations-and-knowledge-connecting-mcp-servers.html))
- **Persistence of tool classification.** Whether `MUTATIVE` / `READ_ONLY` classifications survive infrastructure-as-code updates of the MCP server association, given that unclassified tools default to `READ_ONLY`. ([Directed actions](https://docs.aws.amazon.com/devopsagent/latest/userguide/working-with-devops-agent-working-with-directed-actions.html))
- **Conditional Cedar policies and tool listing.** How argument-conditioned `permit` and `forbid` policies affect which tools appear in `tools/list`. ([Action-scoped policies](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/example-policies-action-resource.html))
- **DevOps Agent tool limits.** The per-call timeout and response size limit for custom MCP tools, which determine output and time budgets.
- **Flux ownership detection.** Which labels or annotations reliably identify Flux-managed objects, so they can be blocked like Argo CD-managed ones.
- **Gateway interceptors.** Whether a gateway supports more than one request or response interceptor, and whether interceptors run before or after policy evaluation. ([Interceptor types](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-interceptors-types.html))
