# Changelog

All notable changes to kube-handrail are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html) as described in [Releasing](docs/releasing.md). Each entry says whether the tool schemas changed (`enable-write` needed) and whether the agent guidance changed (`install-guidance` needed); upgrade steps are in [deploy](docs/deploy.md#upgrading).

## [Unreleased]

Milestones 3 and 4. Implemented, not yet tested on a cluster. Tool schemas: unchanged, so no `enable-write`. Agent guidance: changed (`AGENTS.md`), so re-run `install-guidance`.

### Changed

- **GitOps ownership is detected by controller markers.** A Deployment is `gitops_managed` when it has the Argo CD annotation `argocd.argoproj.io/tracking-id` or a Flux kustomize-controller or helm-controller label (`kustomize.toolkit.fluxcd.io/name|namespace`, `helm.toolkit.fluxcd.io/name|namespace`). `app.kubernetes.io/instance` alone no longer counts: Helm sets it on every release, so every Helm-installed Deployment used to be refused. **Behaviour change:** if your Argo CD uses label tracking, set `gitops_markers = ["app.kubernetes.io/instance"]` before upgrading, or those objects become changeable through kube-handrail. Flux-managed objects, which were not detected before, are now refused.
- **Plan TTL by approval mode.** `plan_ttl_seconds` defaults to `null`: 900 seconds in `client` mode, where the human approves in the chat within the plan's own TTL, and 300 seconds in `server` and `both` mode. A value set in tfvars still wins. The TTL is not a guard; apply checks the live object again ([Guards](docs/guards.md#plan-lifetime)).
- `e2e_gateway.py` reads the values Terraform knows from `terraform output` of a test deployment (other states are ignored); set variables still win. Its `--approval-mode` defaults to the deployment's mode.
- **The gateway request interceptor and the verifier are always deployed.** Legacy mode (`caller_interceptor_enabled = false`) and the `verify_enabled` switch are gone: every plan names its agent, every caller of a write path must be a configured agent, and every apply is verified. The tool Lambdas require `AGENTS` and `AGENT_KILL_SWITCH_PREFIX` and do not start without them; a plan response always has `verification`; preflight no longer reports `agent_enabled` and `agent_budget` as skipped. The sentinel requires `EXPECT_INTERCEPTOR_ARN` and always checks the gateway's interceptor. On a deployment that had both enabled (the default) the upgrade is moves only for these resources; a deployment that had either off gets them in a two-step apply: `terraform apply -target=terraform_data.caller_key -target=aws_iam_role.interceptor -target=aws_lambda_function.interceptor -target=aws_iam_role.verifier`, then `terraform apply`.
- `e2e_gateway.py` runs the verification cases by default; `--skip-verify` leaves them out, and `--with-verify` is accepted and does nothing.

### Added

- `gitops_markers` (Terraform) / `GITOPS_MARKERS` (Lambda environment): extra label or annotation keys that mark GitOps ownership.
- Validation: `plan_ttl_seconds` and `PLAN_TTL_SECONDS` at most 3600.
- Outputs `plan_ttl_seconds`, `guard_settings` and `dashboard_name`.
- A CloudWatch dashboard (with `sentinel_enabled`): plan and apply calls, guard rejections by code, tool errors, sentinel results, verifier runs, backlog and reverts, audit dead letters, failures and lag.
- `terraform/test.tfvars.example`, a test profile from the normal variables, and `e2e_gateway.py --reset-counters`, which deletes the current hour's apply counters on a test deployment only (a `name_prefix` with `test`, `e2e` or `ci` as a word).
- Guidance sync tests (`tests/unit/test_guidance_sync.py`, `scripts/check_contract.py`): the `AGENTS.md` tool and error tables and the `CONTRACT.md` error code line against `tools.json` and the error codes, tool and argument names in the skills, contract links, and word-count ceilings.
- `docs/releasing.md` and this changelog. Approval mode guidance and a shadow-mode recipe in [deploy](docs/deploy.md#approval-mode).

### Deprecated

- `plan_rate_limited`: never returned; it stays in the contract's error code list for this release only.
- Terraform `plan_rate_limit` and `plan_rate_window_seconds`: ignored, with a warning (check `deprecated_plan_rate`) while set. They will be removed in a later release.
- Terraform `caller_interceptor_enabled` and `verify_enabled`: ignored (default `null`), with a warning (check `deprecated_switches`) while set; `false` no longer turns anything off. Remove them from your tfvars; they will be removed in a later release.

### Removed

- Legacy mode and verification off: the `CALLER_INTERCEPTOR` and `VERIFY_ENABLED` Lambda environment variables, and the "verification is not enabled on this server" refusal.
- The per-object plan rate limit. A plan changes nothing, and every apply passes the cooldown, flapping and budget checks, so it added no safety. Preflight still lists `plan_rate`, as `{"passed": true, "skipped": true}`, for this release. The `plan_scale_deployment` tool description still mentions the limit; it is corrected with the next tool schema batch, so this release needs no `enable-write`.

## History before this changelog

Releases before this file were not tagged. They are summarized by milestone; details are in the [roadmap](docs/roadmap.md).

- **Milestone 2: verified changes, more actions, accountable agents.** Restart and rollback by exact revision on a generic plan/apply framework; per-agent identity through a gateway interceptor, with per-agent budgets and kill switches; verification of every apply with an optional pre-approved revert (`get_plan_outcome`); change freezes; a ValidatingAdmissionPolicy that bounds the writer's replica changes; a write-once audit record in S3 with Object Lock.
- **Milestone 1: server-verified write path.** Spec-level conflict detection; the per-object oscillation breaker (cooldown, flapping, budgets, manual change); the drift sentinel and CI contract checks; the measured impact preview and blast-radius budget; server-side approval (`server` and `both` mode, `request_approval`, an IAM-authenticated approval API); correlation IDs and tool metrics; untrusted-output hygiene and `get_workload_events`; `AGENTS.md` and DevOps Agent skills; private-endpoint networking.
- **Milestone 0: vertical slice.** `get_workload_status` and a guarded `plan_scale_deployment` / `apply_scale_deployment` pair behind an AgentCore Gateway, with Terraform, namespace-scoped RBAC, end-to-end guard tests and AWS DevOps Agent registration.
