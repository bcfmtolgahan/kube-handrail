# History checks (oscillation breaker)

Appendix to [the contract](../CONTRACT.md).

History is the set of plan records for one object, read through the `object-history` index for `created_at >= now - (max(3600, FLAP_WINDOW_SECONDS, APPLY_COOLDOWN_SECONDS) + max(PLAN_TTL_SECONDS, APPROVAL_WAIT_TTL_SECONDS) + APPLY_AFTER_APPROVAL_TTL_SECONDS + 60)`, the same span as `retain_until`. An object is `<namespace>/<name>/<uid>`, so a Deployment deleted and recreated under the same name starts with no history. An apply is a record with `result = "applied"`, ordered by `applied_at`; its direction is the sign of `replicas_to - replicas_from`. Windows exclude their start: an event exactly `W` seconds old is outside a window of `W` seconds. Any error reading the history or the last plan fails the request with `internal`; a check is never skipped.

The last apply is also taken from the Deployment itself: the plan named by its `kube-handrail.io/last-plan-id` annotation, read with a consistent `GetItem`, counts as applied when it is in the table, was consumed (`used = true`) and carries the Deployment's `uid`, whatever its recorded `result`. The annotation is set by the apply's own patch and read from the API server, so the most recent apply is counted even when the index has not caught up with it or its result was never recorded; when no `applied_at` was recorded, the latest time the apply can have happened, `min(expires_at, now)`, is used.

| Check | Rule | Code |
|---|---|---|
| Manual change | The Deployment has the annotation `kube-handrail.io/last-plan-id` (a UUID), that plan is in the table, was consumed (`used = true`; its recorded `result` is not required, because only a successful patch sets the annotation) and carries the Deployment's `uid`, and its `replicas_to` differs from the current replica count (unset counts as 1). When that plan left the count unchanged (a restart or rollback), the baseline is instead the `replicas_to` of the most recent apply before it in the object's history that changed the count, so a restart planned over a manual change does not hide it; without one, the restart's own count. Skipped when the annotation is absent or not a UUID, or the plan is no longer in the table or was never consumed; removing the annotation by hand therefore skips it too, which needs the same rights as scaling by hand. `override_manual_change: true` lets the plan or apply go ahead; the plan then reports the check with `"overridden": true`. Scale only. | `manual_change` |
| Manual change, template (rollback only) | The plan named by the annotation (as above) changed the pod template (its `template_from` and `template_to` differ: a rollback or restart), and the Deployment's current template hash differs from that plan's `template_to`: someone changed the template since, typically a deploy rolling forward to a fix. Skipped when that plan left the template as it was (a scale: rolling back a deploy made outside kube-handrail is what a rollback is for), or has no template hashes, and in the same cases as the replica rule. `kubectl rollout undo` replaces the Deployment's whole annotation map with the target ReplicaSet's annotations, which the controller copied from the Deployment when that ReplicaSet was current; an undo to a revision made before kube-handrail's last apply therefore removes the annotation or restores an older plan id, and the rule is skipped or compares against that older plan. A roll-forward by `kubectl apply`, Helm or a patch keeps the annotation. The override works as for the replica rule. | `manual_change` |
| Cooldown | The most recent apply is less than `APPLY_COOLDOWN_SECONDS` old. `0` turns the check off. | `cooldown` |
| Flapping | Among applies in the last `FLAP_WINDOW_SECONDS`, ignoring those that left the count unchanged, take the last two directions. Reject if the requested change reverses the last one and the last one reversed the one before it, i.e. a second reversal within the window. A single revert (up, then back down) is allowed. A request that keeps the count unchanged is never flapping. | `flapping` |
| Object budget | Applies in the last 3600 s `>= OBJECT_HOURLY_APPLY_BUDGET`. The apply being made is not counted. | `object_budget_exceeded` |

`details`, with `history` listing up to the 20 most recent applies in the check's window, oldest first:

```json
{ "code": "flapping", "details": {
    "window_seconds": 600, "count": 2, "requested": { "from": 1, "to": 2 },
    "history": [ { "applied_at": 1700000000, "from": 1, "to": 2 }, { "applied_at": 1700000060, "from": 2, "to": 1 } ] } }
{ "code": "object_budget_exceeded", "details": { "window_seconds": 3600, "limit": 6, "count": 6, "history": [ ... ] } }
{ "code": "manual_change", "details": { "last_applied_to": 2, "current": 3 } }
{ "code": "manual_change", "details": { "last_applied_template": "sha256:...", "current_template": "sha256:..." } }
{ "code": "cooldown", "details": { "cooldown_seconds": 60, "last_applied_at": 1700000060, "retry_after": 1700000120, "history": [ ... ] } }
{ "code": "global_budget_exceeded", "details": { "limit": 30, "window_start": 1699999200, "retry_after": 1700002800 } }
{ "code": "agent_budget_exceeded", "details": { "agent": "smoke_test", "limit": 10, "window_start": 1699999200, "retry_after": 1700002800 } }
```

`count` is the number of applies in the window.

There is no plan rate limit any more. A plan changes nothing and every apply still passes the cooldown, flapping and budget checks, so it limited only how often an agent could ask; `PLAN_RATE_LIMIT` and `PLAN_RATE_WINDOW_SECONDS` are ignored, and `plan_rate_limited` is never returned (it stays in the error code list for one release).

`override_manual_change` is part of `args_hash`, so an approval pins it and apply must pass the same value as the plan (`plan_mismatch` otherwise). `false` and absent are the same.

Known limits:

- Two applies of different plans for one object cannot both succeed: the first patch bumps `generation` (an annotation change alone does), so the second fails apply step 4 with `conflict`, on its read or after a 409. Concurrent applies therefore cannot race the budget or flapping checks; concurrent applies of one plan are serialised by the single-use conditional update.
- The index is eventually consistent (normally well under a second). The last apply is always seen through the annotation; the apply before it is missed only if it is still absent from the index when the next plan and apply both run, which takes two complete plan/apply rounds inside the propagation delay.
- A request that keeps the count unchanged is allowed, has no direction, and once applied counts toward the budget.
- Plans are not rate limited. A refused plan is not stored; each costs one Deployment read, one HPA list and two table reads, and changes nothing.
- Plans that are pending (created but not applied) do not count toward flapping, the cooldown or the budgets.
- A revert ([Revert](verification.md#revert)) is an applied record in the history like any other, with its own direction: it counts toward the cooldown, flapping, and the object budget of later plans, and the Deployment's annotation names it as the last apply. Planning the reverted change again right after the revert is therefore `cooldown`, and after the cooldown `flapping` (a second reversal within the window). The revert itself runs none of these checks and takes no global or agent budget unit.
- The global and per-agent budgets count clock hours, not a sliding window, so up to twice the limit can be applied across an hour boundary.
- The agent unit is taken before the global unit, as two separate conditional updates: an apply refused because the global budget is spent has already used a unit of its agent's budget, which is not given back (like the units of an apply whose patch fails).
- The windows compare `applied_at` and `created_at`, written by one Lambda, with `now` on another; Lambda clocks are NTP-synchronised, so the skew is negligible against windows of minutes.
