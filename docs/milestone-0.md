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

## 1. Sandbox namespace, RBAC and target workload

```sh
export KUBECONFIG=/path/to/test-cluster-kubeconfig
kubectl apply -f k8s/sandbox/
kubectl -n kube-handrail-sandbox rollout status deployment/web
```

This creates:

- `namespace.yaml`: the `kube-handrail-sandbox` namespace (Pod Security `restricted`).
- `rbac.yaml`: Roles and RoleBindings for the groups `kube-handrail-reader` (get/list Deployments and HPAs) and `kube-handrail-writer` (get/patch Deployments). The groups are mapped to the Lambda roles by the EKS access entries created in step 2; nothing outside this namespace is granted.
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

The Deployment controller keeps updating `resourceVersion` while pods start, so an apply right after a rollout may return `conflict`. The test then plans again, up to 3 times. If it fails part-way, `web` may be left at 2 replicas; scale it back with `kubectl` or rerun the test after fixing the cause.

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

### Tool names inside DevOps Agent

DevOps Agent prefixes each tool with the MCP server's registered name and an underscore, for example `kube-handrail_k8sw___apply_scale_deployment`. The 64-character name limit applies to this full name, so keep the server name short. `toolDetails` classifications still use the unprefixed names (`k8sw___apply_scale_deployment`).

## 5. Clean up

```sh
export KUBECONFIG=... AGENT_SPACE_ID=... ASSOCIATION_ID=... SERVICE_ID=...
scripts/cleanup.sh                 # keeps the agent space
scripts/cleanup.sh --delete-space  # also deletes it
```

The script works in reverse order: disassociate, deregister, delete the agent space (only with `--delete-space`), `terraform destroy` in `TF_DIR` (default `terraform/`), then `kubectl delete -f k8s/sandbox`. It asks before each step unless `--yes` is given. Steps whose IDs are not set are skipped, and `--skip-terraform` and `--skip-k8s` skip those steps.
