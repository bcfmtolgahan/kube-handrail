# ADR 0003: Agent identity per OAuth client

- Status: Accepted
- Date: 2026-11-05
- Amends: [ADR 0002](0002-server-verified-approval.md)

Implemented in [Milestone 2](../roadmap.md#milestone-2-verified-changes-more-actions-accountable-agents) (item 3), not yet tested on a cluster; see [Agent identity](../../mcp/CONTRACT.md#agent-identity) for the exact rules.

## Context

Until now every plan was requested by `mcp-gateway`, the class of the gateway's machine identities. The server could not tell which agent asked for a change, so it could neither attribute it nor limit or stop one agent without stopping all of them. Separation of duties in [ADR 0002](0002-server-verified-approval.md) was between "machine" and "human" only.

The caller identity spike (Milestone 1, item 6) showed that a gateway REQUEST interceptor receives the validated token, can overwrite a reserved tool argument with its `client_id`, and that DevOps Agent and another client arrive as different clients. A client that sends the reserved argument itself is overwritten.

## Decision

- **An agent is an OAuth client.** Terraform's `agents` map names each agent with its own Cognito app client (or an existing client of the user pool). The tool Lambdas get the mapping as `AGENTS`, and the interceptor passes the verified `client_id`, signed with a key that only it and the tool Lambdas hold.
- **On by default, fail closed.** The interceptor is attached unless `caller_interceptor_enabled = false`. With it, every path that can lead to a change (`plan_*`, `request_approval`, a gateway `apply_*`) needs a caller listed in `AGENTS`, signed by the interceptor; without one it is refused. The signature is what makes this fail closed: a gateway that lost its interceptor passes a client's own caller argument through, and only the signature tells that value apart. Read tools stay open to any client the gateway accepts. Turning the interceptor off is a documented legacy mode for gateways without interceptors, with no per-agent attribution or limits.
- **Plans name their agent**, as `requested_by = agent:<name>` with the verified `client_id`. Only that agent can request an approval for its plan or apply it through the gateway; the apply's conditional write checks it together with everything else.
- **One channel marker for non-gateway callers.** The approver marks the apply it runs as the `approval-api` channel. That apply carries no caller, is attributed to the plan's agent, and still needs the human approval in the same write. Anything that is not exactly a known channel counts as the gateway, which requires a caller.
- **Per-agent hourly apply budgets and a per-agent kill switch**, next to the global ones. The budget is charged to the plan's agent whichever channel applies it. The switch is an SSM parameter per agent that only operators write; the drift sentinel never does.
- **The drift sentinel checks the gateway's configuration**: the interceptor is attached as deployed and only the expected clients are allowed.

## Consequences

- Separation of duties is between a specific agent and a human approver. `approved_by <> requested_by` is unchanged.
- One agent cannot spend another agent's approved plan, or the budget behind it.
- If the interceptor goes missing, writes stop (fail closed), because no client can sign its own caller argument, and the sentinel reports it. That makes the interceptor part of the write path: one more Lambda per request, and a broken interceptor stops all writes.
- Plans that are open when a deployment is upgraded to identity mode name `mcp-gateway` and must be planned again; their expiry is minutes.
- Two agents that share a client are one agent. Nothing enforces one client per agent.

## Alternatives considered

### A. One gateway per client

Identity follows from which gateway was called, without an interceptor. Rejected as the default: more gateways, targets and registrations to keep in sync. It stays the fallback where interceptors are not available.

### B. The agent in `args_hash`

Making the agent part of the approved arguments would also bind it, but would change every argument hash and mix who asked with what was asked. The consume condition binds the requester instead, so hashes are the same in both modes.

### C. A DynamoDB transaction for the agent and global budgets

Taking both units in one `TransactWriteItems` would never lose an agent unit when the global budget is spent. Rejected as unnecessary: the two units are taken as sequential conditional updates, agent first, and a lost unit is documented, like the unit an apply keeps when its patch fails.
