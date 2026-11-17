# Quickstart

Deploy kube-handrail to an EKS test cluster, test it, and connect AWS DevOps Agent. Use a disposable cluster: the deployment creates IAM roles, EKS access entries and a DevOps Agent registration. For a production account or a private-endpoint cluster, read [Deploying to a new account](deploy.md) first.

## Prerequisites

- An EKS cluster whose authentication mode includes `API`, Kubernetes 1.30 or later, and a cluster-admin kubeconfig ([Prerequisites](deploy.md#prerequisites)).
- AWS credentials in a Region with AWS DevOps Agent and Amazon Bedrock AgentCore.
- Python 3.12, Terraform 1.5 or later, `kubectl`, the AWS CLI, `jq`.

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
```

## 1. Sandbox namespace

```sh
export KUBECONFIG=/path/to/test-cluster-kubeconfig
kubectl apply -f k8s/sandbox/namespace.yaml -f k8s/sandbox/web-deployment.yaml
kubectl -n kube-handrail-sandbox rollout status deployment/web
```

`web` is an nginx with 1 replica, no GitOps marker and no HPA.

## 2. Infrastructure

```sh
cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # set cluster_name, region, namespaces
terraform -chdir=terraform init
terraform -chdir=terraform plan -out=tfplan
terraform -chdir=terraform apply tfplan
kubectl apply --dry-run=server -f build/k8s/ && kubectl apply -f build/k8s/
$(terraform -chdir=terraform output -raw namespace_label_command)
```

Set `approval_mode = "client"` for the first tests: the smoke test applies through the gateway, which `server` mode refuses without an approval. `build/k8s` holds the rendered RBAC and [admission policy](deploy.md#namespaces-rbac-and-admission-policy).

## 3. Smoke test

It runs as the `smoke_test` [agent](deploy.md#agents):

```sh
out() { terraform -chdir=terraform output "$@"; }
secret() { aws cognito-idp describe-user-pool-client --user-pool-id "$(out -raw cognito_user_pool_id)" \
  --client-id "$1" --query UserPoolClient.ClientSecret --output text; }
export GATEWAY_URL=$(out -raw gateway_url) TOKEN_URL=$(out -raw cognito_token_endpoint) SCOPE=$(out -json cognito_scopes | jq -r 'join(" ")')
export CLIENT_ID=$(out -json agent_client_ids | jq -r .smoke_test)
export CLIENT_SECRET=$(secret "$CLIENT_ID")
.venv/bin/python scripts/smoke_test.py
```

It lists the tools (with gateway semantic search on, its search tool appears and this check fails), reads `web`, scales it to 2 and back to 1 through plan and apply, and checks that a plan cannot be used twice. A second run within `flap_window_seconds` (600 s) fails with `flapping`, and a run after scaling `web` by hand fails with `manual_change`: that is the [breaker](guards.md#oscillation-breaker) working. Wait, or remove the annotation:

```sh
kubectl -n kube-handrail-sandbox annotate deployment/web kube-handrail.io/last-plan-id-
```

The end-to-end guard tests are in [Testing](milestone-0.md#guard-tests).

## 4. Register with AWS DevOps Agent

DevOps Agent uses the `devops_agent` client (same shell as step 3):

```sh
export CLIENT_ID=$(out -json agent_client_ids | jq -r .devops_agent)
export CLIENT_SECRET=$(secret "$CLIENT_ID")
R=scripts/devops_agent_register.py
.venv/bin/python $R create-space --name kube-handrail-test          # -> AGENT_SPACE_ID
.venv/bin/python $R register-mcp --name kube-handrail --scope "$SCOPE"  # -> SERVICE_ID
.venv/bin/python $R associate-read                                  # -> ASSOCIATION_ID
.venv/bin/python $R enable-write
.venv/bin/python $R install-guidance
```

Export each printed ID. `associate-read` adds the read tools as `READ_ONLY`; check in chat that the agent can read `web` before `enable-write`, which adds the apply tools as `MUTATIVE` and verifies the classification. `install-guidance` installs [`AGENTS.md`](../AGENTS.md) and the skills. Then set `devops_agent_space_id` and `devops_agent_association_id` in `terraform.tfvars` and apply again, so the [drift sentinel](../mcp/contract/sentinel.md) watches the association.

Start a **new** chat (a chat keeps its first tool list) and ask the agent to scale `web` to 2. DevOps Agent asks for your approval of the apply, pinned to its arguments.

## 5. Server-side approval

Set `approval_mode = "server"` (or `both` with DevOps Agent, [Approval mode](deploy.md#approval-mode)) and `approver_principals` to the IAM users or roles that approve, apply, and attach the `approver_policy_arn` output to each approver (it requires MFA). The agent then plans and calls `request_approval`, and an approver decides:

```sh
export APPROVAL_URL=$(terraform -chdir=terraform output -raw approval_url)
.venv/bin/python scripts/approve.py show <plan_id>
.venv/bin/python scripts/approve.py approve <plan_id>     # in server mode this also applies
.venv/bin/python scripts/approve.py reject <plan_id> --reason "..."
```

## 6. Clean up

```sh
export KUBECONFIG=... AGENT_SPACE_ID=... ASSOCIATION_ID=... SERVICE_ID=...
scripts/cleanup.sh                 # keeps the agent space; --delete-space deletes it
```

It removes the guidance, association and registration, runs `terraform destroy` and deletes `k8s/sandbox` and `build/k8s`, asking before each step unless `--yes`.
