# Milestone 0: deploy the vertical slice to a test cluster

This deploys one read tool and one guarded plan/apply pair to your own EKS test cluster and connects them to AWS DevOps Agent. The tool contract is in [`mcp/CONTRACT.md`](../mcp/CONTRACT.md).

Use a disposable test cluster. The slice only touches the `kube-handrail-sandbox` namespace, but it does create IAM roles, EKS access entries and a DevOps Agent registration in your account.

## Prerequisites

- An EKS cluster whose authentication mode includes `API` (access entries), and a kubeconfig with cluster-admin rights for the setup steps.
- AWS credentials for the target account, selected through the usual `AWS_PROFILE` / `AWS_REGION` variables, in a Region where AWS DevOps Agent and Amazon Bedrock AgentCore are available.
- Python 3.12, Terraform, `kubectl`.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

The offline checks that CI runs on every push (`.github/workflows/ci.yml`):

```sh
.venv/bin/python scripts/check_contract.py   # tools.json, mcp/classification.json and Terraform agree
.venv/bin/python -m pytest -q tests/unit
terraform -chdir=terraform fmt -check -recursive
```

## 1. Sandbox namespace, RBAC and target workload

```sh
export KUBECONFIG=/path/to/test-cluster-kubeconfig
kubectl apply -f k8s/sandbox/
kubectl -n kube-handrail-sandbox rollout status deployment/web
```

This creates:

- `namespace.yaml`: the `kube-handrail-sandbox` namespace (Pod Security `restricted`).
- `rbac.yaml`: Roles and RoleBindings for the groups `kube-handrail-reader` (get/list Deployments, HPAs, and, for the impact preview, PodDisruptionBudgets, Services and EndpointSlices) and `kube-handrail-writer` (get/patch Deployments). The groups are mapped to the Lambda roles by the EKS access entries created in step 2; nothing outside this namespace is granted.
- `web-deployment.yaml`: an unprivileged nginx with 1 replica. Check the image tag against the registry before use, and consider pinning it by digest.

The Deployment deliberately has no `app.kubernetes.io/instance` label, no Argo CD tracking annotation and no HorizontalPodAutoscaler. Any of these makes the plan step refuse it (`gitops_managed`, or the HPA check).

## 2. Infrastructure

Deploy the gateway, Lambdas, DynamoDB plan table, IAM roles, EKS access entries and the kill-switch parameter with Terraform, passing your cluster name, Region, allowed namespaces (`kube-handrail-sandbox`) and replica bounds as variables (copy `terraform/terraform.tfvars.example` to an untracked `terraform.tfvars`):

```sh
cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # then edit
terraform -chdir=terraform init
terraform -chdir=terraform plan -out=tfplan
terraform -chdir=terraform apply tfplan
```

Note these outputs for the next steps:

- `gateway_url`: the gateway MCP endpoint (`GATEWAY_URL`);
- `cognito_token_endpoint`: the token URL (`TOKEN_URL`);
- `cognito_scopes`: the scopes to request (`SCOPE`, space-separated);
- `cognito_client_ids`: the app client IDs (`CLIENT_ID`). Secrets are not output; read the client secret (`CLIENT_SECRET`) from Cognito.

The kill switch is the SSM parameter in the `kill_switch_parameter` output (passed to the write Lambda as `REMEDIATION_ENABLED_PARAM`). It is created as `true`. Applies fail with `remediation_disabled` unless its value is exactly `true`.

Terraform also deploys the [drift sentinel](../mcp/CONTRACT.md#drift-sentinel) (`sentinel_enabled`, default `true`), which checks every 5 minutes that the gateway serves exactly the tools in `mcp/classification.json` and otherwise sets the kill switch to `false` and publishes to the `alert_topic_arn` output (subscribe an address with `alert_email`). Until the DevOps Agent IDs are set (step 4), it checks only the gateway.

Upgrading a deployment made before the [oscillation breaker](guards.md): the plan table gains the `object-history` index and its TTL attribute moves from `expires_at` to `retain_until`. DynamoDB cannot switch the TTL attribute of a table in one step, so replace the table; it only holds short-lived plans, and any plan open at that moment has to be made again:

```sh
terraform -chdir=terraform plan -replace=aws_dynamodb_table.plans -out=tfplan
terraform -chdir=terraform apply tfplan
```

Keep secrets out of shell history, for example by loading them from a local `.env` file (ignored by git).

## 3. Smoke test through the gateway

With the kill switch set to `true`:

```sh
export GATEWAY_URL=... TOKEN_URL=... CLIENT_ID=... CLIENT_SECRET=... SCOPE=...
.venv/bin/python scripts/smoke_test.py
```

The test:

1. checks that `tools/list` returns exactly `k8sr___get_workload_status`, `k8sr___plan_scale_deployment` and `k8sw___apply_scale_deployment`. If semantic search is enabled on the gateway, its built-in search tool also appears and this check fails;
2. reads `web` status and requires 1 replica and no GitOps ownership;
3. plans and applies a scale to 2, then checks that reusing the same `plan_id` returns `plan_used`;
4. waits for 2 ready replicas, then plans and applies the scale back to 1.

The Deployment controller keeps updating status, and with it `resourceVersion`, while pods start. Apply compares `metadata.uid`, `metadata.generation` and `spec.replicas` with the plan rather than `resourceVersion` (status and label changes leave `generation` alone; spec and annotation changes bump it), re-runs the GitOps check, and retries a 409 caused by a status update, so this churn alone no longer produces `conflict`. The test still plans again on `conflict`, up to 3 times; the retry is harmless. If it fails part-way, `web` may be left at 2 replicas; scale it back with `kubectl` or rerun the test after fixing the cause.

Scaling 1 -> 2 -> 1 is a single revert, which the [oscillation breaker](guards.md) allows. Running the smoke test a second time within `FLAP_WINDOW_SECONDS` (default 600 s) of the first makes its first plan, 1 -> 2, a second reversal, and the test fails with `flapping`: that is the breaker working. Wait for the window to pass before running it again. Likewise, after scaling `web` by hand (for example back to 1 replica after a failed run), the next plan is refused with `manual_change`, because the count no longer matches the plan named by the `kube-handrail.io/last-plan-id` annotation. Remove the annotation before running the test again:

```sh
kubectl -n kube-handrail-sandbox annotate deployment/web kube-handrail.io/last-plan-id-
```

### Guard tests

After the smoke test passes, run the end-to-end guard tests against the same deployment:

```sh
export KILL_SWITCH_PARAM=$(terraform -chdir=terraform output -raw kill_switch_parameter)
.venv/bin/python tests/e2e/e2e_gateway.py               # a few minutes
.venv/bin/python tests/e2e/e2e_gateway.py --with-expiry # also waits out one plan TTL
```

They check authentication (missing, malformed and tampered tokens), argument validation (including wrong types, which the gateway rejects against the tool schema), the namespace allowlist and replica bounds, the GitOps and HPA checks, unknown, mismatched, conflicting, concurrent and expired plans, the kill switch, and the oscillation breaker (a change right after an apply is `cooldown`, a single revert is allowed, a second reversal is `flapping`, a manual scale is `manual_change` unless overridden), the blast-radius budget (a plan from 1 to 3 replicas is `blast_radius_exceeded`) and the impact preview (a Service and a PodDisruptionBudget in front of `e2e-target` are reported, and a scale-down past the budget's headroom warns `pdb_headroom`). Set `APPLY_COOLDOWN_SECONDS` to the deployed value; the test waits it out after each apply. The blast-radius case expects the default `MAX_REPLICA_STEP_PERCENT` of 100 and a `max_replicas` of at least 3. The test turns the kill switch off briefly and restores it and applies `tests/e2e/fixtures/argo-managed.yaml` (zero replicas).

The guard tests never change `web`, which belongs to the smoke test; they only plan against it for read-only validation cases that never reach apply. Every case that applies or edits a Deployment uses fixtures created for the run (`e2e-target`, `e2e-flap`, `e2e-manual`: 1-replica unprivileged nginx, plus a Service and a PodDisruptionBudget for `e2e-target`) and deleted at the end, so each run starts with an empty history. The per-object and global budgets and the plan rate limit are covered by unit tests only: reaching them takes 6 or 30 real applies or 20 plans, which is slow and adds nothing the unit tests at the exact limits do not show. The same goes for the `scale_to_zero` and `service_without_endpoints` warnings, which need a plan to 0 replicas: a sandbox with `min_replicas` of 1 refuses that as `out_of_bounds`.

## 4. Register with AWS DevOps Agent

Each step prints the IDs the next step needs as JSON. Client credentials are read from the environment and never printed.

```sh
R=scripts/devops_agent_register.py
.venv/bin/python $R create-space --name kube-handrail-test          # -> agentSpaceId
export AGENT_SPACE_ID=...
.venv/bin/python $R register-mcp --name kube-handrail --scope "$SCOPE"  # -> serviceId
export SERVICE_ID=...
.venv/bin/python $R associate-read                                  # -> associationId
export ASSOCIATION_ID=...
.venv/bin/python $R status
```

`associate-read` associates only the two `k8sr` tools, each classified `READ_ONLY` explicitly. Confirm in chat that the agent can read `web` status and create a plan before enabling writes.

```sh
.venv/bin/python $R enable-write
```

`enable-write` adds `k8sw___apply_scale_deployment` and classifies it `MUTATIVE` in the same `UpdateAssociation` call, because a tool registered without a classification is treated as `READ_ONLY` and runs without approval. It then turns on `elevatedActionsEnabled` on the agent space while keeping its other preferences, and reads both back, exiting non-zero if the tool is not `MUTATIVE` or the preference is not set.

Once `enable-write` has passed, let the drift sentinel watch the association too: set `devops_agent_space_id` and `devops_agent_association_id` in `terraform.tfvars` and apply again. From then on a write tool that is no longer `MUTATIVE`, an unexpected tool, or `elevatedActionsEnabled` turned off turns the kill switch off within one schedule interval. To restore after a drift, follow [Drift sentinel](../mcp/CONTRACT.md#drift-sentinel): fix the cause, re-run `enable-write`, then set the kill switch back to `true` by hand.

Then start a **new** chat and ask the agent to scale `web` in `kube-handrail-sandbox` to 2. A chat keeps the tool list it was created with, so a chat opened before `enable-write` cannot see the apply tool. You can use the console, or the chat script:

```sh
C=scripts/devops_agent_chat.py
.venv/bin/python $C ask "Scale the Deployment 'web' in namespace 'kube-handrail-sandbox' to 2 replicas using the kube-handrail tools: plan it, then apply the plan."
.venv/bin/python $C approve --execution-id <id printed by ask>     # or: reject --reason "..."
```

Expected behavior, observed on a test cluster:

- The agent calls `plan_scale_deployment`, then `apply_scale_deployment`. Instead of running the apply, DevOps Agent returns an approval request with a risk assessment, blast radius, rollback notes and `argument_pins` (the exact `plan_id`, namespace, name and replica count).
- `approve` records a single-use decision with `UpdateApprovalAction`, pinned to exactly those arguments, then resumes the chat with `SendMessage`. The resume context must include `userActionResponse: APPROVAL_ACTION` and the option's `buttonText`; without them DevOps Agent reports the approval as malformed and does not run the tool.
- After approval the apply runs and the Deployment carries `kube-handrail.io/last-plan-id` with the approved plan ID. Replaying the same approval does not run the tool again. A rejected request changes nothing.
- Outside chat, for example in an investigation started from the backlog, DevOps Agent cancels the `MUTATIVE` call before it reaches the gateway ("an approval-ref header is required") and reports the change as a mitigation step for an operator.
- If the write tool is classified `READ_ONLY` by mistake, the agent plans and applies without asking for approval. The server-side limits still apply. Re-run `enable-write` (it verifies the classification) after any change to the association; with the DevOps Agent IDs configured, the drift sentinel also turns the kill switch off when this happens.

### Tool names inside DevOps Agent

DevOps Agent prefixes each tool with the MCP server's registered name and an underscore, for example `kube-handrail_k8sw___apply_scale_deployment`. The 64-character name limit applies to this full name, so keep the server name short. `toolDetails` classifications still use the unprefixed names (`k8sw___apply_scale_deployment`).

## 5. Clean up

```sh
export KUBECONFIG=... AGENT_SPACE_ID=... ASSOCIATION_ID=... SERVICE_ID=...
scripts/cleanup.sh                 # keeps the agent space
scripts/cleanup.sh --delete-space  # also deletes it
```

The script works in reverse order: disassociate, deregister, delete the agent space (only with `--delete-space`), `terraform destroy` in `TF_DIR` (default `terraform/`), then `kubectl delete -f k8s/sandbox`. It asks before each step unless `--yes` is given. Steps whose IDs are not set are skipped, and `--skip-terraform` and `--skip-k8s` skip those steps.
