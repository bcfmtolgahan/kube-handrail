# Restart and rollback

Appendix to [the contract](../CONTRACT.md).

## Rollout history

`get_rollout_history(namespace, name)` lists the revisions of a Deployment that a rollback can target. `namespace` and `name` are validated and the namespace checked against `ALLOWED_NAMESPACES` as for the other tools.

1. Read the Deployment (`not_found` if it does not exist).
2. List the ReplicaSets in the namespace (`apps/v1`, raw JSON) and keep those the Deployment controls, with an `ownerReferences` entry whose `controller` is `true` and whose `uid` is the Deployment's `uid` (stricter than [Workload events](untrusted-output.md#workload-events), which also counts an owner reference without `controller`), and whose `deployment.kubernetes.io/revision` annotation is an integer (ASCII digits, at most 18).
3. Sort newest first by revision, then apply the response limit of [Untrusted output](untrusted-output.md#untrusted-output) (32768 bytes; below).

A failed read of the list is `kubernetes_error`, never an empty list.

```json
{
  "namespace": "sandbox",
  "name": "web",
  "current_revision": 5,
  "revisions": [
    {
      "revision": 5,
      "current": true,
      "replica_set": "web-6c9f8d7b4",
      "created_at": "2026-01-01T12:00:00Z",
      "replicas": 2,
      "ready_replicas": 2,
      "images": [
        { "container": "nginx", "init": false, "image": "<untrusted>nginxinc/nginx-unprivileged:1.27.5-alpine</untrusted>", "pinned_by_digest": false }
      ],
      "change_cause": null
    }
  ],
  "truncated": false
}
```

- `current_revision` is the Deployment's own revision annotation as an integer (`null` when it is not one); `current` is true for the revision equal to it.
- `replica_set` is the ReplicaSet's name; `created_at` its `creationTimestamp` (`null` when absent or not a valid timestamp); `replicas` its `spec.replicas`; `ready_replicas` its `status.readyReplicas` (0 when unset).
- `images` lists the containers and init containers of the ReplicaSet's template, sorted by `init` and then name: `container` (returned plain, because the API server validates container names as DNS-1123 labels), `init`, `image` as untrusted text, and `pinned_by_digest` (the image contains `@sha256:`). At most 20 per revision; more set the envelope's `truncated`.
- `change_cause` is the ReplicaSet's `kubernetes.io/change-cause` annotation as untrusted text, or `null`.
- Nothing else from the template is returned: no env values, commands or args.

Images and the change-cause are untrusted: anyone who can write the Deployment or its ReplicaSets sets them. The response is limited to 32768 bytes of serialized `data` as for events: whole revisions are dropped from the end of the list, the oldest first, until it fits, and `truncated` is then true in `data` and in the envelope. A field cut at its limit also sets the envelope's `truncated`.

Revision numbers last only until the next rollout. A rollback makes the Deployment controller reuse the old ReplicaSet and give it a new number (the highest plus one), so the old number no longer exists, and `revisionHistoryLimit` deletes the oldest ReplicaSets. A revision missing from the list cannot be rolled back to.

## Restart

`plan_restart_deployment(namespace, name)` and `apply_restart_deployment(plan_id, namespace, name)` replace every pod of a Deployment and leave its replica count as it is. The restart sets the pod-template annotation `kube-handrail.io/restart-plan-id` to the plan's `plan_id`: the template the restart leaves is therefore known when the plan is made, pinned in `template_to` and approved with the plan, and every pod the restart creates names the plan. kubectl's `kubectl.kubernetes.io/restartedAt` is not used, so tools that look for it do not see a kube-handrail restart.

Plan, differences from the [scale steps](../CONTRACT.md#guard-checks):

- Checks 1-2 only (`namespace`, `name`, the namespace allowlist); there are no replica bounds.
- Check 4 as for a scale. Then a Deployment with `spec.paused: true` is refused with `conflict`, `details: {"paused": true}`: its rollout would not start, so the plan would replace no pod.
- No HorizontalPodAutoscaler check (an autoscaler owns `spec.replicas`, which a restart does not touch) and no blast-radius budget (see [Guards](../../docs/guards.md#blast-radius-budget)).
- History checks: cooldown, flapping (never triggered: the change has no direction), object budget. No manual-change check: a restart undoes no change. A later scale still compares against the count kube-handrail set before the restart (see Manual change in [History checks](history-checks.md#history-checks-oscillation-breaker)).
- The impact preview is the rollout preview ([Impact preview](impact.md#impact-preview)).
- The record has `replicas_from = replicas_to = ` the current count, `template_to` as above, and `revision_from`.

`data.diff` is `{"restart": {"pods_replaced": <current replicas>}, "template": {"from": "sha256:...", "to": "sha256:..."}}` and `preflight` lists `arguments`, `agent_enabled`, `namespace_allowed`, `action_allowed`, `admission_policy_match`, `not_gitops_managed`, `not_paused`, `cooldown`, `object_budget`, `plan_rate`, `global_budget`, `agent_budget`.

Apply, differences from the [scale steps](../CONTRACT.md#guard-checks):

- Step 2: checks 1-2 only. Step 3: no blast-radius budget. After the consume the record must keep the replica count (`replicas_from = replicas_to`), carry `template_to` and have no override (`plan_mismatch` otherwise).
- Step 4: the same object, GitOps, `generation` and `spec.replicas` checks on every attempt, plus `spec.paused` (`conflict`, `details: {"paused": true}`). An unchanged `generation` means the pod template is still the one the plan hashed.
- Step 7: cooldown, flapping and object budget, then the global budget; no manual-change check.
- The patch is a merge patch (the Python client sends a dict body as `application/strategic-merge-patch+json`) with only `metadata.resourceVersion`, the two annotations of step 6 and the restart annotation:

  ```json
  { "metadata": { "resourceVersion": "<from the read>",
                  "annotations": { "kube-handrail.io/last-plan-id": "<plan_id>", "kube-handrail.io/correlation-id": "<id>" } },
    "spec": { "template": { "metadata": { "annotations": { "kube-handrail.io/restart-plan-id": "<plan_id>" } } } } }
  ```

  The only change to the pod template is the annotation, so the template written is the one hashed as `template_to`.

A successful apply's `data` is `plan_id`, `namespace`, `name`, `pods_replaced` (the planned replica count), `template` (`template_to`), `resource_version`, `approved` and `correlation_id`. The rollout itself is the Deployment controller's: follow it with `get_workload_status`.

## Rollback

`plan_rollback_deployment(namespace, name, revision, override_manual_change?)` and `apply_rollback_deployment(plan_id, namespace, name, revision, override_manual_change?)` set a Deployment's pod template to exactly the template of one of its revisions and leave its replica count as it is. A revision number lasts only until the next rollout ([Rollout history](#rollout-history)), so the plan pins the ReplicaSet the number resolves to and the hash of its template; the number stays the approved argument.

The revision is resolved among the ReplicaSets in the namespace, listed as raw JSON: those the Deployment controls (an owner reference with `controller: true` and the Deployment's `uid`) whose `deployment.kubernetes.io/revision` annotation is an integer equal to `revision`. None is `revision_not_found`, with `details` `{"revision": 3, "available": [5, 4, 2]}` listing the revisions that exist, newest first, at most 20; more than one is `kubernetes_error`. The target template is a copy of that ReplicaSet's raw `spec.template` without the `pod-template-hash` label the controller adds (and without `labels` if nothing else is left in it), as kubectl rollback removes it; `template_to` is its hash, computed as for `template_from`. The API server defaults a Deployment's template and its ReplicaSet's template the same way, so a Deployment rolled back to a ReplicaSet's template hashes equal to it.

Plan, differences from the [scale steps](../CONTRACT.md#guard-checks):

- Checks 1-2, then `revision` (an integer from 1 to 999999999999999999) and `override_manual_change` as for a scale (`invalid_argument` otherwise); there are no replica bounds.
- Check 4 as for a scale. Then a Deployment with `spec.paused: true` is refused with `conflict`, `details: {"paused": true}`, as kubectl refuses it.
- No HorizontalPodAutoscaler check (a rollback leaves `spec.replicas` as it is) and no blast-radius budget, as for a restart.
- The revision is resolved as above. A target template with the hash of the current template is `invalid_argument` ("revision N is the current template").
- History checks: manual change (the template rule in [History checks](history-checks.md#history-checks-oscillation-breaker); the replica rule does not apply, because a rollback leaves the count as it is), cooldown, flapping (never triggered: the change has no direction), object budget.
- The impact preview is the rollout preview, with two warnings only a rollback has ([Impact preview](impact.md#impact-preview)).
- The record has `replicas_from = replicas_to = ` the current count, `template_to` as above, `revision_from`, `revision_to`, `target_rs_name`, `target_rs_uid`, `images`, `fields_changed` and `diff_truncated`.

`data.diff`:

```json
{
  "revision": { "from": 5, "to": 3 },
  "template": { "from": "sha256:...", "to": "sha256:..." },
  "replica_set": "web-7d5b9c6f8",
  "images": [
    { "container": "nginx", "init": false,
      "from": "<untrusted>nginxinc/nginx-unprivileged:1.27.5-alpine</untrusted>",
      "to": "<untrusted>nginxinc/nginx-unprivileged:1.27.4-alpine</untrusted>",
      "changed": true, "pinned_by_digest": false }
  ],
  "fields_changed": ["spec.containers[nginx].env", "spec.containers[nginx].image"]
}
```

- `images` covers every container and init container in either template, matched by name and sorted by `init` and then name. `from` and `to` are the images as untrusted text, `null` for a container in only one of the two templates; `changed` says whether they differ; `pinned_by_digest` is about `to`. At most 20; more set the envelope's `truncated`, and the cut drops unchanged containers first, so a changed image is never hidden behind unchanged ones.
- `fields_changed` names the paths whose values differ, at most two levels below the template's `metadata` and `spec`: `metadata.<key>`, `spec.<key>`, and per container matched by name `spec.containers[<name>].<key>` (likewise `initContainers`), or `spec.containers[<name>]` for a container in only one template, or `spec.containers` when only their order differs. Names only, never values. At most 50; more set the envelope's `truncated`.
- `preflight` lists `arguments`, `namespace_allowed`, `action_allowed`, `admission_policy_match`, `not_gitops_managed`, `not_paused`, `revision_found`, `template_differs`, `no_manual_change` (with `"overridden": true` when the override was used), `cooldown`, `object_budget`, `plan_rate`, `global_budget`, `agent_budget`, with `agent_enabled` after `arguments`.

The template itself, env values, commands and args are never stored in the record or returned. The apply reads the template again from the cluster, so the plan table can never supply a pod spec.

Apply, differences from the [scale steps](../CONTRACT.md#guard-checks):

- Step 2: checks 1-2 and `revision`. Step 3: no blast-radius budget. After the consume the record must keep the replica count, have `revision_to` equal to `revision`, carry `target_rs_name`, `target_rs_uid`, `template_from` and `template_to`, and have the override of the arguments (`plan_mismatch` otherwise).
- Step 4, on every attempt: the same object, GitOps, `generation` and `spec.replicas` checks, then `spec.paused` (`conflict`, `details: {"paused": true}`), then the ReplicaSet named `target_rs_name` is read again as raw JSON (the writer needs `get` on ReplicaSets for this):
  - not found: `revision_not_found`, `details: {"revision": 3, "replica_set": "<name>"}`;
  - its `uid` is not `target_rs_uid`, or it has no controller owner reference to the Deployment: `conflict`, `details: {"replica_set": {"planned", "current"}}`;
  - the hash of its template, computed as at plan time, is not `template_to`: `conflict`, `details: {"template": {"planned", "current"}}`.

  The revision annotation is not checked again: an unchanged `generation` means no rollout happened since the plan, so the number cannot have moved.
- Step 7: the template manual-change rule (the Deployment's template is still the one hashed as `template_from`, because `generation` is unchanged), cooldown, flapping and object budget, then the global budget.
- The patch is a JSON Patch (the Python client sends a list body as `application/json-patch+json`), with the ops in this order:

  ```json
  [
    { "op": "replace", "path": "/metadata/resourceVersion", "value": "<from the read>" },
    { "op": "replace", "path": "/spec/template", "value": <the template just hashed and compared> },
    { "op": "add", "path": "/metadata/annotations/kube-handrail.io~1last-plan-id", "value": "<plan_id>" },
    { "op": "add", "path": "/metadata/annotations/kube-handrail.io~1correlation-id", "value": "<id>" }
  ]
  ```

  The resourceVersion op makes the API server refuse the patch with 409 if the Deployment changed after the read, as the resourceVersion in the merge patches does; a 409 retries from step 4 and reads both objects again. `add` on an existing annotation replaces it; a Deployment without annotations gets one `add` of `/metadata/annotations` with both. The template written is exactly the one hashed and compared. Unlike kubectl, the ReplicaSet's annotations (such as `kubernetes.io/change-cause`) are not copied to the Deployment: the write stays limited to `spec.template` and the two annotations.

A successful apply's `data` is `plan_id`, `namespace`, `name`, `revision_requested`, `template` (`template_to`), `images` (as in the plan), `resource_version`, `approved` and `correlation_id`. The Deployment's new revision number is not reported: the controller assigns it afterwards, reusing the target ReplicaSet with the next number. Follow the rollout with `get_workload_status`.

A revision is only as trustworthy as the namespace's RBAC ([Guards](../../docs/guards.md#rollback)). A ConfigMap or Secret the template reads keeps its current content, and an image tag can have been moved since the revision ran.
