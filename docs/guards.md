# Guards

Why each server-side check exists. The exact rules, settings and error `details` are in the [contract](../mcp/CONTRACT.md#guard-checks) and its [limits table](../mcp/CONTRACT.md#limits).

## Layers

An agent can be wrong, misled by text in the cluster, or misconfigured, and so can its client. kube-handrail does not rely on any one check. Three independent layers stand between an agent and a Deployment, and each assumes the others may fail:

1. **The guard in the write Lambda** is authoritative. It is the only layer that knows the plan, the approval and the history, so it checks everything.
2. **Kubernetes RBAC** limits the writer to `get` and `patch` on what its namespaces allow. A bug in the guard cannot delete or create anything.
3. **The admission policy** refuses a writer patch outside the namespace's replica bounds, even if the guard let it through. Every plan first checks that the policy is in place and matches, because a backstop nobody checks can be switched off silently ([ADR 0006](adr/0006-admission-policy.md)).

Budgets and the blast radius stay global, not per namespace: they limit the total damage of one deployment.

## Approval modes

An approval is a guard only when the server checks it. In `server` and `both` mode the approval is part of the apply's own conditional write, pinned to the exact arguments, and the approver must not be the requester. In `client` mode the server cannot see the chat approval, and CloudTrail redacts the plan ID from it, so the approver cannot be proven. That is why `client` is for evaluation ([deploy](deploy.md#approval-mode), [ADR 0002](adr/0002-server-verified-approval.md)).

## Plan lifetime

The plan TTL is not a guard. It only bounds how long an approval can refer to a plan. Apply checks the live object again whatever the plan's age, so a stale plan is refused, not applied. `client` mode gets a longer default TTL because the human approves in the chat within the plan's own lifetime, with nothing to extend it.

## GitOps ownership

A change to an object a GitOps controller owns would be reverted at the next sync, or would fight it. Such changes belong in Git, so plan and apply refuse them. The markers are the ones the controllers set themselves. `app.kubernetes.io/instance` alone is not one, because Helm sets it on every release and treating it as GitOps refused every Helm-installed Deployment; teams on Argo CD label tracking add it to `gitops_markers`.

## Blast-radius budget

Each change may move the replica count by at most a share of the current count, whatever the approval says. A human approving a plan at 2 a.m. can misread a number; the budget makes a typo a small mistake. Bigger changes take several steps, and each step passes the breaker.

A restart or rollback has no step budget. It keeps the replica count; its blast radius is that every pod is replaced, at the pace of the Deployment's own `maxUnavailable`. A server-wide "at most N pods down" limit would either refuse most single-replica Deployments or limit nothing. Instead the approver sees the rollout in the impact preview, with a warning for `Recreate`.

## Impact preview

The approver should decide on facts, not on the agent's description of them. A plan therefore reports what it touches, measured when it is made: pods added or removed, PodDisruptionBudget headroom, Service endpoints now and projected. A scale-down is not an eviction, so a PodDisruptionBudget will not stop it; the preview says so. Warnings never block a plan, because the human decides. If any object cannot be read, the plan fails rather than show a partial picture ([Impact preview](../mcp/contract/impact.md)).

## Rollback

A rollback writes a whole pod template: images, env, volumes, service account. So the template comes only from a ReplicaSet the Deployment controls, pinned by `uid` and hash at plan time and checked again at apply. A ValidatingAdmissionPolicy cannot compare a template with a ReplicaSet's, so this hash check is the only control for "exactly that template".

A revision is only as trustworthy as the namespace's RBAC. Anyone who can edit ReplicaSets there can make one look like a revision. That person could already change the Deployment, so this adds no capability, but the approver should read the images and changed fields as facts about that ReplicaSet, not as proof of what ran before.

## Oscillation breaker

Two actors who disagree about a Deployment, an agent and a human or two agents, can change it back and forth forever, and each change on its own looks reasonable. The breaker looks at what kube-handrail already did to the object ([History checks](../mcp/contract/history-checks.md)):

- **Flapping.** One revert is often the right fix, so up then down is allowed. A second reversal within the window is a fight, not a fix.
- **Cooldown.** The effect of a change must have time to show before the next one.
- **Per-object and global budgets.** Many small changes can add up to a big one. The global budget caps that across all objects.
- **Manual change.** If someone changed the object outside kube-handrail since its last apply, an agent must not undo that silently. Undoing it needs `override_manual_change: true`, which is part of the approved arguments, so the approver sees it.

A revert counts as a change, so re-planning a change right after its revert is refused: a change that failed once should not be retried by an agent without a human. There is no plan rate limit: a plan changes nothing, and every apply passes the checks above.

History is read from the plan table and from the Deployment's own `kube-handrail.io/last-plan-id` annotation, because the index is eventually consistent and only a successful patch sets the annotation. If the history cannot be read, the request fails; the checks are never skipped.

## Per-agent budget and kill switch

Every plan names the agent that asked for it, so one misbehaving agent can be slowed down or stopped without stopping the others. An apply is charged to the agent that planned it, also when the approval channel runs it, and only that agent can request approval for its plan or apply it, so one agent cannot spend another's approved plan. The drift sentinel never writes an agent's switch: turning an agent back on is a deliberate human step.

## Verify and revert

An approved change can still fail: pods that do not start, a quota that refuses them. Every apply is therefore checked once, after a window the plan names, from the server's own reading. A human can approve a scale together with its revert (`revert_on_failure`); then a `degraded` change is set back once, and an alarm tells a human ([ADR 0004](adr/0004-pre-approved-revert.md)).

- The revert never runs on `unknown`: if someone else changed the object, the server does not know what to restore.
- It is stopped by the global kill switch, the bounds and GitOps ownership, which protect the cluster.
- It is not stopped by the breaker, the budgets or the agent's kill switch, and takes no budget: it is the inverse of one change that already passed them, and refusing it would leave the object in the state the human chose to undo.
- It is verified like any change, and never reverted itself.

The check is one reading, not a comparison with the state before the change, so a Deployment that was already failing can be found degraded; the plan warns about it (`revert_baseline_unhealthy`).

## Change freezes

Releases, migrations and holidays need a time when nothing changes. A freeze is checked on every plan and again at apply with the apply's own time, so an approval given before a freeze is not applied during it. An unreadable freeze list refuses writes, like an unreadable kill switch. A pre-approved revert is exempt: it undoes a change the human approved together with its revert. There is no break-glass path; an operator shortens the freeze ([ADR 0005](adr/0005-change-freezes.md)).

```sh
scripts/freeze.py add --namespace <namespace> --until 2026-12-27T00:00:00Z --reason "release freeze"
scripts/freeze.py list
scripts/freeze.py remove 0
```

## Audit record

The plan table can be changed by the write role, so it cannot be its own evidence. Every change to a plan record is also written once to an S3 bucket with Object Lock that the tool Lambdas cannot write to. The record detects after the fact and blocks nothing: a change is never refused because the record could not be written, because a broken audit pipeline must not stop remediation. `scripts/audit.py verify` reports rewrites and gaps ([Audit record](../mcp/contract/audit.md), [ADR 0007](adr/0007-durable-audit-record.md)).
