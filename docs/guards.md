# Oscillation breaker

Two actors who disagree about a Deployment (an agent and a human, or two agents) can scale it back and forth indefinitely, and each change on its own looks reasonable. The breaker looks at what kube-handrail has already done to the same object and refuses the next change when the pattern is wrong. The exact rules, settings and error `details` are in [`mcp/CONTRACT.md`](../mcp/CONTRACT.md#history-checks-oscillation-breaker).

- **Flapping.** Scaling up and then back down is allowed: one revert is often the right fix. A second reversal of direction within `FLAP_WINDOW_SECONDS` (default 10 minutes), for example up, down, up, is refused with `flapping`.
- **Cooldown.** At least `APPLY_COOLDOWN_SECONDS` (default 60) between two applies to one Deployment, so the effect of a change can show before the next one (`cooldown`).
- **Per-object budget.** At most `OBJECT_HOURLY_APPLY_BUDGET` applies (default 6) to one Deployment in any hour (`object_budget_exceeded`).
- **Global budget.** At most `GLOBAL_HOURLY_APPLY_BUDGET` applies (default 30) across all Deployments in one clock hour (`global_budget_exceeded`). It caps the damage of many small changes that each pass the per-object checks. Apply counts against it with one atomic update, after every other check has passed.
- **Plan rate limit.** At most `PLAN_RATE_LIMIT` plans (default 20) for one Deployment in `PLAN_RATE_WINDOW_SECONDS` (default 10 minutes), so an agent stuck in a loop stops at the planning step (`plan_rate_limited`).
- **Manual change.** kube-handrail marks each Deployment it changes with the plan it applied. If the replica count no longer matches that plan, someone changed it by other means, and a plan that would change it again is refused with `manual_change` unless it sets `override_manual_change: true`. The flag is part of the approved arguments, so the approver sees it.

History is kept per object, identified by namespace, name and `uid`: deleting and recreating a Deployment starts over. All checks run when planning, for early feedback, and again when applying (except the rate limit), which is the decision that counts. If the history cannot be read, the request fails; the checks are never skipped. Each rejection lists the recent applies so the agent and the approver can see why.

The history index is eventually consistent and the outcome of an apply is recorded after the patch, so the most recent apply is also taken from the Deployment itself: the plan named by its `kube-handrail.io/last-plan-id` annotation counts as applied whatever the table says about it, because only a successful patch sets that annotation. Two applies cannot race each other on one object either: the first bumps the Deployment's `generation`, which makes the second a `conflict`. A no-op request (the count stays the same) is allowed and counts toward the budget. Rejected plans are not stored, so they are not rate-limited; they change nothing and cost a few reads.

Not covered yet: per-agent limits, which need the caller's identity on the server. See the [roadmap](roadmap.md).
