# Guards

Server-side checks that hold whatever the client, the agent or the approver does. The exact rules, settings and error `details` are in [`mcp/CONTRACT.md`](../mcp/CONTRACT.md#guard-checks).

## Layers

Three independent checks stand between an agent and a Deployment; each assumes the others may fail.

1. **The guard in the write Lambda** (this page): authoritative. It checks every plan and apply against the namespace, the replica bounds, the blast radius, the breaker, the budgets, the approval and the kill switches.
2. **Kubernetes RBAC**: the writer group can only `get` and `patch` Deployments and `get` ReplicaSets, in the sandbox namespace only (`k8s/sandbox/rbac.yaml`).
3. **The admission policy** `kube-handrail-writer-bounds` (`k8s/sandbox/admission-policy.yaml`): in the cluster, a writer patch that changes the replica count to a value outside the bounds is refused even if the guard let it through (`admission_denied`). It applies only to the writer group in the sandbox namespace. It starts in `Warn` and `Audit` and is switched to `Deny` after a check ([deploy](deploy.md#admission-policy)). See [Admission policy](../mcp/CONTRACT.md#admission-policy) and [ADR 0006](adr/0006-admission-policy.md).

The policy checks only the replica bounds. It does not check the pod template, the approval or the history, which need state that only the guard has.

## Approval modes

The approval is a guard only when the server checks it. In `server` (the default) and `both` mode the write Lambda refuses an apply without an unexpired approval, by a listed human other than the requester, of exactly the planned arguments, and records that human on the plan. In `client` mode the only approval is the client's (the DevOps Agent chat): the server requires none and cannot bind it to a plan, because CloudTrail records the chat approval with every argument, the plan ID included, redacted. Use `server`, or `both` with DevOps Agent in production; `client` is for evaluation ([deploy](deploy.md#approval-mode)).

## Plan lifetime

A plan is valid for `PLAN_TTL_SECONDS`. When `plan_ttl_seconds` is not set, Terraform picks 900 seconds in `client` mode, where the human reads the plan and approves it in the DevOps Agent chat with nothing to extend it, and 300 seconds in `server` and `both` mode, where `request_approval` extends it to `approval_wait_ttl_seconds`. The TTL is capped at 3600 (implemented, not yet tested on a cluster).

The TTL is not a guard, and a longer one does not mean 15 minutes in which anything goes. It only bounds how long an approval can refer to a plan. Apply checks the live object again whatever the plan's age: the same `uid` and `generation` and the planned replica count (`conflict` otherwise), the per-object and global budgets, the cooldown and flapping rules, the change freezes and the kill switches. A plan that has gone stale is refused at apply, not applied.

## GitOps ownership

An object a GitOps controller owns is changed in Git, not here: plan and apply refuse it with `gitops_managed`. The markers are the ones the controllers set: the Argo CD annotation `argocd.argoproj.io/tracking-id`, and the Flux labels `kustomize.toolkit.fluxcd.io/name|namespace` and `helm.toolkit.fluxcd.io/name|namespace`. Malformed metadata counts as managed.

`app.kubernetes.io/instance` alone is not a marker: Helm sets it on every release, and treating it as GitOps refused every Helm-installed Deployment. Argo CD's label tracking writes only that label, so a team using it adds it to `gitops_markers` (any label or annotation key listed there counts as a marker). This changed in the current release; before it, the label was a default marker. Implemented, not yet tested on a cluster. Details: [guard check 4](../mcp/CONTRACT.md#guard-checks).

## Blast-radius budget

A single plan may change a Deployment's replica count by at most `MAX_REPLICA_STEP_PERCENT` (default 100) of its current count, rounded up and never less than 1. At the default, a Deployment at 1 replica can go to 0 or 2, one at 3 to anything from 0 to 6. A larger change is refused with `blast_radius_exceeded`, both when planning and again when applying, with the setting in force at apply time; an approval does not lift it. Bigger changes take several steps, each of which also passes the oscillation breaker below.

A restart or a rollback has no step budget. It keeps the replica count, so the replica step is always 0; its blast radius is that every pod is replaced, at the pace of the Deployment's own rollout strategy. The cap that matters for that is already in the Deployment: `maxUnavailable` for a rolling update. A server-wide limit such as "at most N pods unavailable at once" would either refuse most single-replica Deployments or, set loosely enough not to, limit nothing. Instead the approver sees the rollout as measured facts in the impact preview (below), including a warning for the `Recreate` strategy, which stops every pod before starting new ones; that is a warning, not a refusal, because restarting a single-replica `Recreate` workload is a common and legitimate need. How often a Deployment can be restarted or rolled back is bounded by the cooldown and the per-object and global budgets of the oscillation breaker, like any other change.

## Impact preview

A plan reports what the change touches, measured when the plan is made, so the approver sees facts rather than the agent's description of them: the pods added or removed, the PodDisruptionBudgets that select the Deployment's pods and how many disruptions each allows now, and for each Service in front of the pods its ready endpoints now and a projection after the change. Warnings flag a scale to zero, a scale-down that removes more pods than a PodDisruptionBudget allows (a scale-down is not an eviction, so the budget itself will not stop it), and a Service projected to have no ready endpoints. For a restart the preview also shows the rollout strategy with the pods that can be unavailable or added at once, and each Service's ready endpoints at the lowest point of the rollout; warnings then flag the `Recreate` strategy, a rollout that can take more pods down at once than a PodDisruptionBudget allows, a Service left without endpoints during the rollout, a rollout that has not finished yet, and a Deployment with no pods. A rollback's preview is a restart's, and also flags a target image that is not pinned by digest (the tag may now point to other content) and a target template that reads ConfigMaps or Secrets, whose current content a rollback keeps. Warnings never block a plan; they are for the human deciding. If any of these objects cannot be read, the plan fails instead of showing a partial picture. See [Impact preview](../mcp/CONTRACT.md#impact-preview).

## Rollback

A rollback writes a whole pod template, which can change images, env, volumes and the service account, far more than a replica count. The template comes only from a ReplicaSet the Deployment controls: the plan pins that ReplicaSet's `uid` and the hash of its template, and the apply reads it again and writes exactly the template it hashed, or refuses the change if the ReplicaSet was removed (`revision_not_found`), replaced or edited (`conflict`). The approver sees the image of each container before and after, and the names of the other fields that change, never their values. See [Rollback](../mcp/CONTRACT.md#rollback).

A revision is only as trustworthy as the namespace's RBAC. Anyone who can create or edit ReplicaSets in the namespace can make one with a controller owner reference to the Deployment and a revision annotation, and have it offered as a revision to roll back to. Such a person could already change the Deployment itself, so this adds no capability, but the approver should read the images and changed fields as facts about that ReplicaSet, not as proof of what ran before. An edit made after the plan is caught by the hash check; one made before it is what the plan shows. A ValidatingAdmissionPolicy cannot compare a written template with a ReplicaSet's, so the hash check in the guard is the only control for "exactly that template".

## Oscillation breaker

Two actors who disagree about a Deployment (an agent and a human, or two agents) can scale it back and forth indefinitely, and each change on its own looks reasonable. The breaker looks at what kube-handrail has already done to the same object and refuses the next change when the pattern is wrong. The exact rules are in [History checks](../mcp/CONTRACT.md#history-checks-oscillation-breaker).

- **Flapping.** Scaling up and then back down is allowed: one revert is often the right fix. A second reversal of direction within `FLAP_WINDOW_SECONDS` (default 10 minutes), for example up, down, up, is refused with `flapping`.
- **Cooldown.** At least `APPLY_COOLDOWN_SECONDS` (default 60) between two applies to one Deployment, so the effect of a change can show before the next one (`cooldown`).
- **Per-object budget.** At most `OBJECT_HOURLY_APPLY_BUDGET` applies (default 6) to one Deployment in any hour (`object_budget_exceeded`).
- **Global budget.** At most `GLOBAL_HOURLY_APPLY_BUDGET` applies (default 30) across all Deployments in one clock hour (`global_budget_exceeded`). It caps the damage of many small changes that each pass the per-object checks. Apply counts against it with one atomic update, after every other check has passed.
- **No plan rate limit.** Earlier versions also limited plans per Deployment (`plan_rate_limited`). A plan changes nothing, and every apply passes the cooldown, flapping and budget checks above, so the limit added no safety and only slowed tests; it was removed (implemented, not yet tested on a cluster). The code stays in the contract for one release and is never returned.
- **Manual change.** kube-handrail marks each Deployment it changes with the plan it applied. If the replica count no longer matches that plan, someone changed it by other means, and a plan that would change it again is refused with `manual_change` unless it sets `override_manual_change: true`. The flag is part of the approved arguments, so the approver sees it. A restart undoes no change, so it is not checked. It keeps whatever count it finds, so a later scale still compares against the count kube-handrail set before the restart: a manual change made before a restart is still caught. A rollback is checked against the pod template instead: if kube-handrail's last apply changed the template (a rollback or restart) and the template is no longer the one it left, someone rolled forward since, and rolling back over that needs the override too. After a scale the template rule does not apply: rolling back a deploy made by someone else is what a rollback is for. The mark is an annotation on the Deployment, and `kubectl rollout undo` replaces all of its annotations with those of the revision it restores, so after such an undo the rule may not see kube-handrail's last apply; a roll-forward by `kubectl apply`, Helm or a patch keeps the mark.

A restart or rollback counts like any other apply for the cooldown and the budgets. It leaves the replica count unchanged, so it has no direction: it is never flapping and does not affect the flapping check of later scales. A restart's pod template carries a new annotation each time, so it never reverses anything either. Rolling back and forth between two templates has no flapping rule of its own: every rollback needs a human approval, and the cooldown and the budgets bound how often it can happen.

A revert ([below](#verify-and-revert)) counts as an apply too, with its own direction. Planning the reverted change again right after the revert is therefore refused with `cooldown`, and after the cooldown with `flapping`: a change that failed once should not be retried by an agent without a human.

History is kept per object, identified by namespace, name and `uid`: deleting and recreating a Deployment starts over. All checks run when planning, for early feedback, and again when applying (except the rate limit), which is the decision that counts. If the history cannot be read, the request fails; the checks are never skipped. Each rejection lists the recent applies so the agent and the approver can see why.

The history index is eventually consistent and the outcome of an apply is recorded after the patch, so the most recent apply is also taken from the Deployment itself: the plan named by its `kube-handrail.io/last-plan-id` annotation counts as applied whatever the table says about it, because only a successful patch sets that annotation. Two applies cannot race each other on one object either: the first bumps the Deployment's `generation`, which makes the second a `conflict`. A no-op request (the count stays the same) is allowed and counts toward the budget. Rejected plans are not stored, so they are not rate-limited; they change nothing and cost a few reads.

## Per-agent budget and kill switch

Each agent is one OAuth client, and every plan names the agent that asked for it ([Agent identity](../mcp/CONTRACT.md#agent-identity)). That makes two limits per agent possible, next to the global ones, so one misbehaving agent can be slowed down or stopped without stopping the others.

- **Per-agent budget.** At most `hourly_apply_budget` applies (default `AGENT_HOURLY_APPLY_BUDGET`, 10) by one agent in one clock hour (`agent_budget_exceeded`). An apply is charged to the agent that planned it, also when the approval channel runs it. The global budget still applies; the agent's unit is taken first, so an apply refused by the global budget has used up a unit of its agent's budget as well.
- **Per-agent kill switch.** An SSM parameter per agent, `/<name_prefix>/agents/<name>/enabled`. Anything but the exact value `true`, or a parameter that cannot be read, stops that agent's plans, approval requests and applies at once (`remediation_disabled` with `details.scope = "agent"`), including the apply that an approval runs for a plan the agent made. The global kill switch still stops every agent. The drift sentinel never writes these switches, so turning an agent back on is always a deliberate step.

Both are checked when planning, for early feedback, and again when applying, which is the decision that counts. Only the agent that planned a change can request its approval or apply it through the gateway, so one agent cannot spend another agent's approved plan or the budget behind it. Without the caller interceptor (legacy mode) every plan names the gateway instead of an agent, and neither limit applies.

To stop one agent: `aws ssm put-parameter --overwrite --name /<name_prefix>/agents/<name>/enabled --value false`; set it back to `true` to let it act again.

## Verify and revert

An approved change is checked once, after a window the plan names (`verify_window_seconds`, default 120 seconds), from the server's own reading of the Deployment: `healthy` if it reached its target, `degraded` if not, `unknown` if the reading cannot tell, for example because someone else changed the Deployment since. The apply schedules the check in the same write that consumes the plan, and a scheduled verifier runs it through an entry point of the write Lambda that no MCP client can reach. `get_plan_outcome` reports it. See [Verification and revert](../mcp/CONTRACT.md#verification-and-revert).

A scale plan can also pin `revert_on_failure`, with an explicit window, so the human approves the change and its revert together. If the check finds the change `degraded`, the server sets replicas back to the count the plan started from, once, and an alarm tells a human. The revert:

- runs only on `degraded`, never on `unknown`, and only when the Deployment is still exactly as the apply left it: the same object, still marked with the plan, with the spec the apply wrote. The patch carries the resourceVersion of that same reading;
- is stopped by the global kill switch, the replica bounds and GitOps ownership. A kill switch that is off when the check runs skips the revert for good: by the time it is back on, the approved window has passed;
- is not stopped by the agent's own kill switch, the blast-radius budget, the oscillation breaker, the budgets or the manual-change check, and takes no budget unit. It is the inverse of one change that already passed them, and refusing it would leave the object in the state the human chose to undo;
- is verified like any change, and never reverted itself.

The check is one reading at the end of the window, not a comparison with the state before the change. A Deployment that was already short of available pods can be found degraded for a cause the change did not bring; the plan warns about it (`revert_baseline_unhealthy`). The reasons for this design are in [ADR 0004](adr/0004-pre-approved-revert.md).

## Change freezes

Status: implemented and tested on a test cluster.

An operator can freeze changes in one or more namespaces, or all of them, for a window: over a release, a migration or a holiday. The windows live in one SSM parameter, `/<name_prefix>/freezes`, which `scripts/freeze.py` lists and edits; each window names its namespaces, a start, an end (`until`, excluded), a reason and optionally the actions it covers (`scale`, `restart`, `rollback`). See [Change freezes](../mcp/CONTRACT.md#change-freezes).

- A plan in a frozen namespace is refused with `change_frozen`, before the cluster is read. The refusal carries the reason and the moment changes are allowed again, so the agent can tell the human.
- An apply is checked again with its own time, before the plan is consumed. An approval given before a freeze is therefore not applied during it, whether the agent applies or the approval's own apply runs it. The plan is left unconsumed and expires unless the freeze ends first.
- A parameter that cannot be read, or that does not validate, refuses every write (`remediation_disabled` with `details.scope = "freezes"`), as an unreadable kill switch does. `scripts/freeze.py` validates every write with the Lambdas' own code, and `remove --all` resets a broken value.
- A pre-approved revert is not stopped by a freeze, and the verification still runs: the revert undoes a change a human approved together with its revert. Only the global kill switch stops reverts.
- There is no break-glass path. To make a change during a freeze, an operator removes or shortens the freeze; the reasons are in [ADR 0005](adr/0005-change-freezes.md).

```sh
scripts/freeze.py add --namespace <namespace> --until 2026-12-27T00:00:00Z --reason "release freeze"
scripts/freeze.py list
scripts/freeze.py remove 0
```
## Audit record
Every change to a plan record is also written, once, to an S3 bucket with Object Lock, by a separate Lambda that reads the plan table's stream. The tool Lambdas, the approver and the verifier cannot write to or delete from the bucket. `scripts/audit.py export <plan_id>` shows who asked for a change, who approved it, the channel it ran under, the result and the verification and revert outcome, after the plan record itself has expired. Implemented and tested on a test cluster.
The record detects after the fact; it does not block. A change is never refused because the record could not be written; the failure alarms instead. A plan record changed by the executor is not refused either: the change becomes another entry next to the original ones, and nothing flags it automatically. See [Audit record](../mcp/CONTRACT.md#audit-record) and [ADR 0007](adr/0007-durable-audit-record.md).
