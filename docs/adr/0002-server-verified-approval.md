# ADR 0002: Server-verified approval

- Status: Accepted
- Date: 2026-10-27
- Amends: [ADR 0001](0001-scope.md)

Accepted as the design for the write path. Implementation is planned in [Milestone 1](../roadmap.md#milestone-1-server-verified-write-path) (item 5); nothing in this ADR is implemented yet.

## Context

Milestone 0 deployed one guarded plan/apply pair to a test cluster and connected it to AWS DevOps Agent ([Milestone 0](../milestone-0.md)). The server-side guard behaved as designed, but testing showed that the human approval step exists only in the client:

- **Approval lives only in the client.** The server never sees an approval. `apply_scale_deployment` accepts any call that carries a valid, unused, unexpired plan with matching arguments, whoever or whatever made it.
- **A misclassified write tool loses its approval.** With the write tool classified `READ_ONLY` by mistake, the agent planned and applied without asking anyone. The server-side limits (namespace allowlist, replica bounds, GitOps and HPA checks, single-use plans, kill switch) still held, but no human was involved.
- **Other MCP clients may have no equivalent step.** The design targets any MCP client. A client without an approval UI, or one configured to auto-approve, would apply within server limits without a human.
- **Autonomous investigations cannot remediate.** Outside chat, DevOps Agent cancels a `MUTATIVE` call before it reaches the gateway ("an approval-ref header is required") and reports the change as a mitigation step. An investigation started from an alert can propose a change but has no path to get it approved and applied.
- **DevOps Agent approvals are pinned to exact arguments.** The approval request carries `argument_pins` (plan ID, namespace, name, replica count), and the recorded decision is single-use and bound to those values. This is the property worth preserving; the problem is only that the server cannot verify it.

A second constraint comes from the guard itself. Plans are pinned to `metadata.resourceVersion`, which the Deployment controller keeps changing while pods start ([Milestone 0](../milestone-0.md), smoke test). A plan that waits minutes for a human would almost always fail with `conflict`.

## Decision

Make the human approval a fact the server stores and verifies, pinned to the plan's exact arguments, instead of a step the server has to trust the client to have performed.

### Plan lifecycle

A plan record moves through these states:

```
planned -> awaiting_approval -> approved -> applied
                 |                  |
                 +-> rejected       +-> expired
                 +-> expired
```

- `planned`: created by a `plan_*` tool, as today.
- `awaiting_approval`: an approval was requested (see `request_approval` below).
- `approved` / `rejected`: a human decided through an approval channel.
- `applied`: consumed by a successful apply. `expired`: a TTL elapsed first.

The plan gains `requested_by` (the requesting principal) and, once approved, an approval record: `approved_by` (principal), `approved_at`, `approved_args_hash`, `approval_channel` and `approval_expires_at` (see Split TTLs).

### Apply consumes only an approved plan

In `server` mode, the single conditional update that consumes a plan today

```
used = false AND expires_at > :now AND args_hash = :h AND action = :a
```

is extended so that the same update also requires an approval:

```
... AND attribute_exists(approved_by)
    AND approved_args_hash = args_hash
    AND approved_by <> requested_by
    AND approval_expires_at > :now
```

Approval, argument match, separation of duties and single use are checked in one atomic write, so there is no window between "check approval" and "consume plan". A missing or mismatched approval returns the new error code `approval_required`.

**Separation of duties.** The approver principal must differ from the requester principal. Until per-agent caller identity reaches the server (Milestone 1, item 6), this is enforced by principal class: requests come from machine identities (OAuth client credentials), approvals only from authenticated human identities, so the two can never be the same principal. Per-agent attribution will make `requested_by` specific without changing the rule.

### Approvals come from outside MCP

Approvals are accepted only through an authenticated, human-facing channel that is not part of the MCP surface:

- a Slack interactive message, accepted only when the request signature is verified and the Slack user maps to an allowed approver;
- a CLI or API call authenticated as a human identity with MFA.

Machine identities (client credentials) can never approve, regardless of their scopes. The approval channel renders the plan from the server's own record (arguments, current and target state), not from text supplied by the agent.

In `server` mode the apply after approval is triggered from the approval channel, not by the agent, so a client that cannot invoke `MUTATIVE` tools outside chat can still complete the change. That apply goes through the same guard as an apply through the gateway.

### `APPROVAL_MODE`

| Mode | Behavior |
|---|---|
| `client` | Today's flow. The client (DevOps Agent directed actions) approves; the server does not require an approval record. |
| `server` | An approval record is required for every apply, whatever the client did. For all clients other than DevOps Agent chat, and for autonomous investigations. |
| `both` | Both are required: the client approval and a server approval record. |

`client` keeps the DevOps Agent chat flow unchanged (one approval, no double prompt). `server` is the default for every other client.

### `request_approval(plan_id)`

A new `READ_ONLY` tool that moves a plan to `awaiting_approval` and notifies approvers.

- Destinations (Slack channel, webhook) are configured on the server. The agent cannot choose or supply a destination.
- Calls are rate limited per plan and globally, so an agent cannot flood approvers.
- It does not approve anything; it only asks. Calling it repeatedly cannot change the outcome.

This lets an autonomous investigation plan a change and ask for approval without ever calling a `MUTATIVE` tool.

### Split TTLs

- **Awaiting approval:** about 15 minutes. Long enough for a human to respond. This depends on replacing the `resourceVersion` precondition with a spec-level precondition on `metadata.generation` and `spec.replicas` (Milestone 1, item 1); status-only churn must not invalidate a plan while it waits.
- **Apply after approval:** about 2 minutes. Once approved, the change must happen promptly or be planned and approved again.

### MCP elicitation is not an approval channel

MCP elicitation asks the user a question through the client. It is rejected as an approval channel because the answer travels through the same client and agent session that requested the change; a prompt-injected agent, or a client that answers automatically, could supply it. It may be used for clarifying questions, never for approval.

## Consequences

- **New human-facing endpoint.** The approval channel (Slack callback, CLI/API) is a new authenticated surface and a high-value target. It needs human identity with MFA, signature verification, a short list of allowed approvers, and its own audit trail.
- **Cedar is not on the approval-triggered path.** An apply started from the approval channel does not pass through the gateway, so the Cedar policy layer does not see it. The write guard and RBAC still apply, as will the admission policy once it exists. This is documented rather than hidden, and the guard remains the authoritative check.
- **Generation precondition is a prerequisite.** The awaiting-approval TTL is only workable with spec-level conflict detection. Milestone 1 orders the work accordingly.
- **ADR 0001 wording changes.** The scope stays as decided. The statement that writes are approved in the client becomes: every write is covered by a human approval pinned to its exact arguments and verified by the server.
- **Client mode still trusts the client.** In `client` mode a misclassified tool still loses its approval. The classification drift sentinel (Milestone 1, item 3) turns the kill switch off when classification drifts, and `server` or `both` mode removes the dependency.
- **Residual risks.**
  - A prompt-injected agent can still request approval for a change with a misleading justification. Mitigations: the approver sees server-rendered arguments and a measured impact preview (Milestone 1, item 4), and the blast-radius budget applies independently of approval.
  - Approval fatigue: humans who approve everything. Rate limiting and per-object oscillation limits reduce volume but do not remove the risk.
  - Compromise of an approver account or of the Slack workspace. Mitigated by MFA, approver allowlists and audit, not eliminated.
  - Until per-agent identity is available, separation of duties is between "machine" and "human", not between specific agents.

## Alternatives considered

### A. Keep approval in the client only

Rely on DevOps Agent directed actions and equivalent features in other clients.

Rejected as the only mechanism. Milestone 0 showed that a single misclassification removes the approval, other clients may not have an equivalent step, and autonomous investigations cannot remediate. Kept as `APPROVAL_MODE=client` for DevOps Agent chat.

### B. MCP elicitation

Ask the user for approval through the MCP client.

Rejected. The answer comes back through the agent's own session; an injected agent or an auto-answering client could approve its own change.

### C. Use DevOps Agent's approval reference as the gate

DevOps Agent requires an approval reference before running a `MUTATIVE` call. Whether that reference reaches the gateway, and whether the server could verify it, is untested. Even if it does, it only covers DevOps Agent chat. If it reaches the server it may be recorded as evidence, but it is not used as a gate.

### D. An `approve_plan` MCP tool

Expose approval as a tool on the gateway.

Rejected. Any MCP client, including the requesting agent, could call it, which makes self-approval possible. Classifying it `MUTATIVE` only moves the problem back to client classification.

### E. Encode approval in Cedar policy

Cedar policies are stateless and evaluate a single request; they cannot hold a per-plan, single-use approval fact. Cedar keeps its role of argument limits at the gateway.
