# ADR 0005: Change freezes

- Status: Accepted
- Date: 2026-11-06

Implemented in [Milestone 2](../history/milestones.md#milestone-2-verified-changes-more-actions-accountable-agents) (item 5, freezes only); see [Change freezes](../../mcp/contract/freezes.md#change-freezes) for the exact rules.

## Context

Organisations stop changes at known times: a release, a migration, the end of the year. An agent that follows its runbook does not know about them, and a human approving a single plan may not remember. The guard is the one place every change passes, on every channel, including the apply that a server approval runs and that the gateway never sees. Neither the gateway's policy language nor Kubernetes admission has a clock, so a window in time can only be enforced there.

## Decision

- **Windows in one SSM parameter**, next to the kill switch: a JSON list of absolute windows in UTC, each with namespaces (or `*`), `start` (included), `until` (excluded), a reason and optionally the actions it covers. Operators edit it out of band with `scripts/freeze.py`, which validates with the Lambdas' own code. Terraform creates it empty and ignores later changes, as it does for the kill switches. Weekly or recurring windows are not supported; an operator adds absolute windows.
- **Checked on plan and again on apply**, on every action, read without caching. The apply check uses the time of the apply and runs before the plan is consumed: an approval given before a freeze is not applied during it, and the refused plan is left unconsumed, so it can still be applied if the freeze ends while it is valid, and otherwise expires. Consuming it would turn a temporary refusal into a lost approval for no gain; nothing reaches the cluster either way.
- **Fail closed like the kill switch.** A parameter that cannot be read or does not validate refuses every write with `remediation_disabled` (`details.scope = "freezes"`), the code an unreadable kill switch gives. A stale window is valid and inactive, so an old entry never blocks anything.
- **A pre-approved revert is exempt**, as [ADR 0004](0004-pre-approved-revert.md) exempts it from the agent's own kill switch. It undoes one change a human approved together with its revert; refusing it during a freeze would leave the object in the state the human chose to undo. It never enters the apply path. The verification changes nothing and is not affected either.
- **No break-glass.** A change that cannot wait is made by an operator removing or shortening the freeze, a deliberate act by someone with write access to the parameter, visible in its history. A ticket and a second approver inside the tool flow would add a second approval path, its own state and its own failure modes, for what is in practice a rare, human-driven decision.
- **`request_approval` and the approval API do not read freezes.** The apply that follows an approval does, which is the decision that counts.

## Consequences

- A freeze takes effect on the next call, for every agent and both approval channels, without a deployment.
- During a freeze, approvals can still be recorded, but their applies are refused; approvers see `change_frozen` in the approval API's response.
- A broken parameter stops all writes until it is fixed (`scripts/freeze.py remove --all`). This is the same trade-off as the kill switch.
- SSM has no conditional write, so two operators editing the freezes at the same moment can lose one edit; `scripts/freeze.py list` shows what is stored.
