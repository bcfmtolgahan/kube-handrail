# Guards

Server-side checks that hold whatever the client, the agent or the approver does. The exact rules, settings and error `details` are in [`mcp/CONTRACT.md`](../mcp/CONTRACT.md#guard-checks).

## Blast-radius budget

A single plan may change a Deployment's replica count by at most `MAX_REPLICA_STEP_PERCENT` (default 100) of its current count, rounded up and never less than 1. At the default, a Deployment at 1 replica can go to 0 or 2, one at 3 to anything from 0 to 6. A larger change is refused with `blast_radius_exceeded`, both when planning and again when applying, with the setting in force at apply time; an approval does not lift it. Bigger changes take several steps, each of which also passes the oscillation breaker below.

A restart has no step budget. It keeps the replica count, so the replica step is always 0; its blast radius is that every pod is replaced, at the pace of the Deployment's own rollout strategy. The cap that matters for that is already in the Deployment: `maxUnavailable` for a rolling update. A server-wide limit such as "at most N pods unavailable at once" would either refuse most single-replica Deployments or, set loosely enough not to, limit nothing. Instead the approver sees the rollout as measured facts in the impact preview (below), including a warning for the `Recreate` strategy, which stops every pod before starting new ones; that is a warning, not a refusal, because restarting a single-replica `Recreate` workload is a common and legitimate need. How often a Deployment can be restarted is bounded by the cooldown and the per-object and global budgets of the oscillation breaker, like any other change.

## Impact preview

A plan reports what the change touches, measured when the plan is made, so the approver sees facts rather than the agent's description of them: the pods added or removed, the PodDisruptionBudgets that select the Deployment's pods and how many disruptions each allows now, and for each Service in front of the pods its ready endpoints now and a projection after the change. Warnings flag a scale to zero, a scale-down that removes more pods than a PodDisruptionBudget allows (a scale-down is not an eviction, so the budget itself will not stop it), and a Service projected to have no ready endpoints. For a restart the preview also shows the rollout strategy with the pods that can be unavailable or added at once, and each Service's ready endpoints at the lowest point of the rollout; warnings then flag the `Recreate` strategy, a rollout that can take more pods down at once than a PodDisruptionBudget allows, a Service left without endpoints during the rollout, a rollout that has not finished yet, and a Deployment with no pods. Warnings never block a plan; they are for the human deciding. If any of these objects cannot be read, the plan fails instead of showing a partial picture. See [Impact preview](../mcp/CONTRACT.md#impact-preview).

## Oscillation breaker

Two actors who disagree about a Deployment (an agent and a human, or two agents) can scale it back and forth indefinitely, and each change on its own looks reasonable. The breaker looks at what kube-handrail has already done to the same object and refuses the next change when the pattern is wrong. The exact rules are in [History checks](../mcp/CONTRACT.md#history-checks-oscillation-breaker).

- **Flapping.** Scaling up and then back down is allowed: one revert is often the right fix. A second reversal of direction within `FLAP_WINDOW_SECONDS` (default 10 minutes), for example up, down, up, is refused with `flapping`.
- **Cooldown.** At least `APPLY_COOLDOWN_SECONDS` (default 60) between two applies to one Deployment, so the effect of a change can show before the next one (`cooldown`).
- **Per-object budget.** At most `OBJECT_HOURLY_APPLY_BUDGET` applies (default 6) to one Deployment in any hour (`object_budget_exceeded`).
- **Global budget.** At most `GLOBAL_HOURLY_APPLY_BUDGET` applies (default 30) across all Deployments in one clock hour (`global_budget_exceeded`). It caps the damage of many small changes that each pass the per-object checks. Apply counts against it with one atomic update, after every other check has passed.
- **Plan rate limit.** At most `PLAN_RATE_LIMIT` plans (default 20) for one Deployment in `PLAN_RATE_WINDOW_SECONDS` (default 10 minutes), so an agent stuck in a loop stops at the planning step (`plan_rate_limited`).
- **Manual change.** kube-handrail marks each Deployment it changes with the plan it applied. If the replica count no longer matches that plan, someone changed it by other means, and a plan that would change it again is refused with `manual_change` unless it sets `override_manual_change: true`. The flag is part of the approved arguments, so the approver sees it. A restart undoes no change, so it is not checked.

A restart counts like any other apply for the cooldown and the budgets. It leaves the replica count unchanged, so it has no direction: it is never flapping and does not affect the flapping check of later scales. Its pod template carries a new annotation each time, so it never reverses anything either.

History is kept per object, identified by namespace, name and `uid`: deleting and recreating a Deployment starts over. All checks run when planning, for early feedback, and again when applying (except the rate limit), which is the decision that counts. If the history cannot be read, the request fails; the checks are never skipped. Each rejection lists the recent applies so the agent and the approver can see why.

The history index is eventually consistent and the outcome of an apply is recorded after the patch, so the most recent apply is also taken from the Deployment itself: the plan named by its `kube-handrail.io/last-plan-id` annotation counts as applied whatever the table says about it, because only a successful patch sets that annotation. Two applies cannot race each other on one object either: the first bumps the Deployment's `generation`, which makes the second a `conflict`. A no-op request (the count stays the same) is allowed and counts toward the budget. Rejected plans are not stored, so they are not rate-limited; they change nothing and cost a few reads.

Not covered yet: per-agent limits, which need the caller's identity on the server. See the [roadmap](roadmap.md).
