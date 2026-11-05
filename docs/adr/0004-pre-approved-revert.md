# ADR 0004: Pre-approved revert

- Status: Accepted
- Date: 2026-11-06
- Amends: [ADR 0002](0002-server-verified-approval.md)

Implemented in [Milestone 2](../roadmap.md#milestone-2-verified-changes-more-actions-accountable-agents) (item 4), not yet tested on a cluster; see [Verification and revert](../../mcp/CONTRACT.md#verification-and-revert) for the exact rules.

## Context

An applied change was not checked afterwards: the agent was told to read the status again, and nothing recorded whether the change reached its target. When a change made things worse, undoing it needed a new plan, a new approval and an agent that noticed, usually while the incident was still going on.

ADR 0002 makes every change a human approval pinned to exact arguments. An automatic undo looks like a change nobody approved, and the guards that bound every change (the oscillation breaker and the budgets) would refuse it right after the change it undoes.

## Decision

- **Verify every apply.** The apply schedules a check, `verify_window_seconds` after it, in the same write that consumes the plan. A scheduled verifier runs it through an internal entry point of the write Lambda that the gateway cannot reach, and records `healthy`, `degraded` or `unknown`. `get_plan_outcome` reports it to agents.
- **The human approves the change and its revert together.** `revert_on_failure` is an argument of a scale plan, pinned in the approval like every other argument, and it needs an explicit window, so the approver sees both values. A revert runs only for such a plan, only when its verification is `degraded`, and at most once (its record id is derived from the plan's).
- **The revert restores exactly the approved starting state**, under the spec-level precondition: the Deployment must still be the object the apply changed, annotated with that plan, at the generation and replica count the apply left, and the patch carries the resourceVersion of that reading. Any change by someone else since makes the verification `unknown`, which never reverts.
- **What stops a revert:** the global kill switch, the replica bounds, GitOps ownership and the precondition. Not the agent's own kill switch, the blast-radius budget, the oscillation breaker, the budgets or the manual-change check. A revert takes no budget unit.
- **A revert is never reverted.** Its record never carries `revert_on_failure`, and the verifier skips records of kind `revert`. The revert is verified like any change, for the record.
- **A revert counts as a change afterwards.** It is an applied record in the history, so planning the reverted change again right after it is refused with `cooldown`, then `flapping`.
- **Records the server makes name a system requester** (`system:revert`). The approver refuses to decide on them.

## Consequences

- A degraded change approved with revert is undone within about a minute of its window, without an agent or a second approval, and a human is alerted.
- The guards are deliberately narrower for a revert. The exemption is bounded: exactly the inverse of one approved change, only after a `degraded` verdict, only under the precondition, once per plan, and never for a revert.
- Disabling a misbehaving agent does not block the undo of a change a human approved with its revert. Stopping reverts needs the global kill switch.
- The verdict is one reading at the end of the window. A brief dip at that moment, or a failure that existed before the change, can make it `degraded`; the impact preview warns when the Deployment is already short of available pods, and the human chooses the window.
- A kill switch that is off when the check runs skips the revert for good: by the time it is back on, the approved window has passed. An unreadable switch is retried until the grace period ends.

## Alternatives considered

### A. Revert as a new plan that a human approves

Safe, but slow when it matters most, and it depends on someone noticing. The approval of the original change already covers the revert when the human asked for it.

### B. Run the revert through the breaker and the budgets

A revert refused by the cooldown or an exhausted budget leaves the object in the state the human chose to undo. The revert is the inverse of a change that already passed these checks.

### C. Cancel a change and its revert for later flapping checks

Re-planning the same change right after its revert would then be allowed. Rejected: a change that degraded once should not be retried by an agent without a human, and the cooldown and flapping refusals make that explicit.

### D. EventBridge Scheduler one-shots, Step Functions or SQS for the timed check

Each needs a new VPC endpoint and new grants on the write role, and leaves a gap after the patch where the schedule can fail to be created. A scheduled sweeper over a sparse index needs neither and recovers from a missed run on its own.
