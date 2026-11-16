# Changelog

All notable changes to kube-handrail are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) as described in [Releasing](docs/releasing.md). Each entry says whether the tool schemas changed (`enable-write` needed) and whether the agent guidance changed (`install-guidance` needed); `make upgrade` runs both when needed ([deploy](docs/deploy.md#upgrading)).

## [Unreleased]

Milestone 3: implemented and tested on a test cluster, except the test profile and `--reset-counters`, which are implemented but not yet run. Milestone 4 (legacy mode and the verify switch removed, `make upgrade`): implemented and tested on a test cluster (`make upgrade` from Milestone 3 took 110 seconds; the plan showed only the 12 moves and package rebuilds). Tool schemas: unchanged, so no `enable-write`. Agent guidance: changed (`AGENTS.md` and both skills), so re-run `install-guidance` (`make upgrade` does). Milestone 6 (namespace policy, per-namespace approvers, rendered cluster manifests, plan-time admission check, the plan's cluster): implemented, not yet tested on a cluster; the rendered admission policy and RBAC were checked on kind. The tool schemas do not change in Milestone 6.

### Changed

- **GitOps ownership is detected by controller markers.** A Deployment is `gitops_managed` when it has the Argo CD annotation `argocd.argoproj.io/tracking-id` or a Flux kustomize-controller or helm-controller label (`kustomize.toolkit.fluxcd.io/name|namespace`, `helm.toolkit.fluxcd.io/name|namespace`). `app.kubernetes.io/instance` alone no longer counts: Helm sets it on every release, so every Helm-installed Deployment used to be refused. **Behaviour change:** if your Argo CD uses label tracking, set `gitops_markers = ["app.kubernetes.io/instance"]` before upgrading, or those objects become changeable through kube-handrail. Flux-managed objects, which were not detected before, are now refused.
- **Plan TTL by approval mode.** `plan_ttl_seconds` defaults to `null`: 900 seconds in `client` mode, where the human approves in the chat within the plan's own TTL, and 300 seconds in `server` and `both` mode. A value set in tfvars still wins. The TTL is not a guard; apply checks the live object again ([Guards](docs/guards.md#plan-lifetime)).
- `e2e_gateway.py` reads the values Terraform knows from `terraform output` of a test deployment (other states are ignored); set variables still win. Its `--approval-mode` defaults to the deployment's mode.
- **The gateway request interceptor and the verifier are always deployed.** Legacy mode (`caller_interceptor_enabled = false`) and the `verify_enabled` switch are gone: every plan names its agent, every caller of a write path must be a configured agent, and every apply is verified. The tool Lambdas require `AGENTS` and `AGENT_KILL_SWITCH_PREFIX` and do not start without them; a plan response always has `verification`; preflight no longer reports `agent_enabled` and `agent_budget` as skipped. The sentinel requires `EXPECT_INTERCEPTOR_ARN` and always checks the gateway's interceptor. On a deployment that had both enabled (the default) the upgrade is moves only for these resources; a deployment that had either off gets them in a two-step apply ([deploy](docs/deploy.md#upgrading)).
- `request_approval` in `client` mode still returns `invalid_argument`, with a clearer message: "client mode: apply directly, your client asks the human", followed by the previous message, so agents with the previous guidance still recognize it.
- The drift sentinel leaves the kill switch as it is when it already holds any value other than `true` (which is already off), instead of overwriting it with `false`; this keeps the marker `make upgrade` writes while the tool set changes.
- `e2e_gateway.py` runs the verification cases by default; `--skip-verify` leaves them out, and `--with-verify` is accepted and does nothing.
- The per-release upgrade notes moved to [Upgrade notes before Milestone 4](docs/history/upgrading.md); [deploy](docs/deploy.md#upgrading) describes `make upgrade`.

### Added

- **Namespace policy** (Milestone 6). Terraform `namespaces` maps each namespace to its `min_replicas`, `max_replicas`, `actions` (`restart`, `rollback`, `scale`), `approvers` and `gitops_markers`. It ships in the tool Lambda and approver packages as `namespace_policy.json` (in their source hashes); `ALLOWED_NAMESPACES` is derived from it. An action the namespace does not allow is `forbidden_namespace` with `details` `{action, allowed_actions}`, on plan and apply; no new error code.
- **Per-namespace approvers.** A namespace with its own `approvers` is decided only by them (`forbidden`, `details.reason` `not_namespace_approver`, for anyone else, global approvers included); the others by `approver_principals`. The approver URL's permissions and Terraform's checks cover both lists.
- **Rendered cluster manifests** (`terraform/k8s.tf`, `terraform/modules/namespace-policy`): `build/k8s` holds per-namespace Roles and RoleBindings from the actions, one admission policy with the bounds of every namespace as CEL maps keyed by `request.namespace`, its binding in `admission_mode` (`Deny`, default, or `Warn`), and the ClusterRole `kube-handrail-policy-reader`. No kubernetes provider. Outputs `namespaces`, `k8s_manifests`, `namespace_label_command`.
- **Plan-time admission check.** Every plan reads the admission policy and its binding and is refused with `conflict` (`details.check` `admission_policy_match`) unless the binding denies, both select the namespace and the bounds match; an unreadable policy fails closed. Preflight lists `action_allowed` and `admission_policy_match`. Only `admission_check = false` (read Lambda `ADMISSION_CHECK`) turns it off.
- **The plan's cluster.** Plans record `cluster` (`CLUSTER_NAME`); the approval view, the notification ("Cluster: ..."), `get_plan_outcome`, the audit summary and `scripts/approve.py` show it.
- `make upgrade UPGRADE_FLAGS=--with-k8s` (`--kube-context`): writes `build/k8s`, applies it after a server-side dry run, checks the binding and lists Roles no longer rendered. `make upgrade` runs `terraform get` before planning.
- `tests/k8s_render`: golden files for the rendered manifests, checked in CI without AWS. `tests/admission/run.sh` covers two namespaces with different bounds and the policy reader. `e2e_gateway.py --with-namespaces` (`SECOND_NAMESPACE`).
- `make upgrade` (`scripts/upgrade.py`): one resumable upgrade command. It refuses to start while the kill switch is off unless it turned it off itself, refuses a plan that deletes or replaces the gateway, its targets, Cognito, the plan table, the audit bucket or the kill switch and freezes parameters, applies known two-step migrations first, turns the kill switch off while the tool set changes, retries an "inconsistent final plan" once, runs `enable-write` and `install-guidance` only when needed, requires a sentinel `ok`, and ends with the smoke test. `--dry-run` stops after the plan check. Also `make bootstrap`, `deploy`, `register`, `smoke` and `destroy` (`scripts/cleanup.sh`).
- `scripts/smoke_test.py --read-only`: the read path only, for `server` and `both` mode.
- Outputs `gateway_id`, `approval_mode`, `devops_agent_space_id`, `devops_agent_association_id`, `tool_set_hash` and `cognito_user_pool_id`.
- `terraform/backend.tf.example`: remote state in S3 with a lock file.
- `gitops_markers` (Terraform) / `GITOPS_MARKERS` (Lambda environment): extra label or annotation keys that mark GitOps ownership.
- Validation: `plan_ttl_seconds` and `PLAN_TTL_SECONDS` at most 3600.
- Outputs `plan_ttl_seconds`, `guard_settings` and `dashboard_name`.
- A CloudWatch dashboard (with `sentinel_enabled`): plan and apply calls, guard rejections by code, tool errors, sentinel results, verifier runs, backlog and reverts, audit dead letters, failures and lag.
- `terraform/test.tfvars.example`, a test profile from the normal variables, and `e2e_gateway.py --reset-counters`, which deletes the current hour's apply counters on a test deployment only (a `name_prefix` with `test`, `e2e` or `ci` as a word).
- Guidance sync tests (`tests/unit/test_guidance_sync.py`, `scripts/check_contract.py`): the `AGENTS.md` tool and error tables and the `CONTRACT.md` error code line against `tools.json` and the error codes, tool and argument names in the skills, contract links, and word-count ceilings.
- `docs/releasing.md` and this changelog. Approval mode guidance and a shadow-mode recipe in [deploy](docs/deploy.md#approval-mode).

### Deprecated

- Terraform `sandbox_namespace`, `min_replicas` and `max_replicas` (now default `null`): replaced by `namespaces`. While `namespaces` is empty they still define the one namespace, with a warning (check `deprecated_namespace_variables`); together with `namespaces` they are refused. They will be removed in a later release.
- `k8s/sandbox/rbac.yaml` and `k8s/sandbox/admission-policy.yaml`: superseded by `build/k8s`; do not apply them. They will be deleted once the test harnesses that read them move.
- `plan_rate_limited`: never returned; it stays in the contract's error code list for this release only.
- Terraform `plan_rate_limit` and `plan_rate_window_seconds`: ignored, with a warning (check `deprecated_plan_rate`) while set. They will be removed in a later release.
- Terraform `caller_interceptor_enabled` and `verify_enabled`: ignored (default `null`), with a warning (check `deprecated_switches`) while set; `false` no longer turns anything off. Remove them from your tfvars; they will be removed in a later release.

### Removed

- The `sed` recipes for the RBAC and the admission policy in the deploy guide.
- Legacy mode and verification off: the `CALLER_INTERCEPTOR` and `VERIFY_ENABLED` Lambda environment variables, and the "verification is not enabled on this server" refusal.
- The per-object plan rate limit. A plan changes nothing, and every apply passes the cooldown, flapping and budget checks, so it added no safety. Preflight still lists `plan_rate`, as `{"passed": true, "skipped": true}`, for this release. The `plan_scale_deployment` tool description still mentions the limit; it is corrected with the next tool schema batch, so this release needs no `enable-write`.

### Upgrade notes (Milestone 6)

Implemented, not yet tested on a cluster.

1. `make upgrade UPGRADE_FLAGS=--with-k8s` with a kubeconfig for the cluster that may create Roles, a ClusterRole and the admission policy (cluster-admin). It prints the kubectl context before asking. Without `--with-k8s`, apply `build/k8s` yourself right after the upgrade (`kubectl apply --dry-run=server -f build/k8s/ && kubectl apply -f build/k8s/`): until then every plan is refused with `conflict` (`admission_policy_match`), because the read Lambda checks the new policy format through the new ClusterRole.
2. The ClusterRole `kube-handrail-policy-reader` is new: `get` on the admission policy and its binding only, bound to the reader group. It is the reader's first cluster-scoped right; review it.
3. Run the label command: `$(terraform -chdir=terraform output -raw namespace_label_command)`.
4. With `namespaces` empty nothing changes in behaviour: the deprecated variables define the same namespace and bounds, with every action, and the rendered binding is in `Deny`. The plan rebuilds the packages, updates every Lambda in place (the read Lambda gains `ADMISSION_CHECK`), creates `terraform_data.k8s_manifests` and changes outputs; Terraform warns while the deprecated variables are set. To move to `namespaces`, copy the entry the warning prints.
5. A deployment that kept its binding in `Warn` must either move to `Deny` (the default `admission_mode`) or set `admission_check = false`.

## History before this changelog

Releases before this file were not tagged. They are summarized by milestone; details are in the [roadmap](docs/roadmap.md).

- **Milestone 2: verified changes, more actions, accountable agents.** Restart and rollback by exact revision on a generic plan/apply framework; per-agent identity through a gateway interceptor, with per-agent budgets and kill switches; verification of every apply with an optional pre-approved revert (`get_plan_outcome`); change freezes; a ValidatingAdmissionPolicy that bounds the writer's replica changes; a write-once audit record in S3 with Object Lock.
- **Milestone 1: server-verified write path.** Spec-level conflict detection; the per-object oscillation breaker (cooldown, flapping, budgets, manual change); the drift sentinel and CI contract checks; the measured impact preview and blast-radius budget; server-side approval (`server` and `both` mode, `request_approval`, an IAM-authenticated approval API); correlation IDs and tool metrics; untrusted-output hygiene and `get_workload_events`; `AGENTS.md` and DevOps Agent skills; private-endpoint networking.
- **Milestone 0: vertical slice.** `get_workload_status` and a guarded `plan_scale_deployment` / `apply_scale_deployment` pair behind an AgentCore Gateway, with Terraform, namespace-scoped RBAC, end-to-end guard tests and AWS DevOps Agent registration.
