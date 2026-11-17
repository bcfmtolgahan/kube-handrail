# Quickstart

Deploy kube-handrail to an EKS test cluster, test it, and connect AWS DevOps Agent. Use a disposable cluster: the tools act only in `kube-handrail-sandbox`, but the deployment creates IAM roles, EKS access entries and a DevOps Agent registration. For a production account or a private-endpoint cluster, read [Deploying to a new account](deploy.md) first.

## Prerequisites

- An EKS cluster whose authentication mode includes `API`, Kubernetes 1.30 or later, and a cluster-admin kubeconfig ([Prerequisites](deploy.md#prerequisites)).
- AWS credentials in a Region with AWS DevOps Agent and Amazon Bedrock AgentCore.
- Python 3.12, Terraform 1.5 or later, `kubectl`.

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

`web` is an unprivileged nginx with 1 replica, no GitOps marker and no HorizontalPodAutoscaler.

## 2. Infrastructure

```sh
cp terraform/terraform.tfvars.example terraform/terraform.tfvars   # set cluster_name, region, namespaces
terraform -chdir=terraform init
terraform -chdir=terraform plan -out=tfplan
terraform -chdir=terraform apply tfplan
kubectl apply --dry-run=server -f build/k8s/ && kubectl apply -f build/k8s/
$(terraform -chdir=terraform output -raw namespace_label_command)
```

Set `approval_mode = "client"` for the first tests: the smoke test applies through the gateway, which `server` mode refuses without an approval. `build/k8s` holds the RBAC and the admission policy Terraform rendered ([Namespaces, RBAC and admission policy](deploy.md#namespaces-rbac-and-admission-policy)).

The outputs you need next: `gateway_url` (`GATEWAY_URL`), `cognito_token_endpoint` (`TOKEN_URL`), `cognito_scopes` (`SCOPE`) and the `smoke_test` client in `agent_client_ids` (`CLIENT_ID`); read its secret (`CLIENT_SECRET`) from Cognito. Keep secrets out of shell history, for example in an ignored `.env` file.

## 3. Smoke test

```sh
export GATEWAY_URL=... TOKEN_URL=... CLIENT_ID=... CLIENT_SECRET=... SCOPE=...
.venv/bin/python scripts/smoke_test.py
```

It lists the tools, reads `web`, scales it to 2 and back to 1 through plan and apply, and checks that a plan cannot be used twice. A second run within `flap_window_seconds` (600 s) fails with `flapping`, and a run after scaling `web` by hand fails with `manual_change`: that is the [breaker](guards.md#oscillation-breaker) working. Wait, or remove the annotation:

```sh
kubectl -n kube-handrail-sandbox annotate deployment/web kube-handrail.io/last-plan-id-
```

The end-to-end guard tests are in [Testing](milestone-0.md#guard-tests).

## 4. Register with AWS DevOps Agent

```sh
R=scripts/devops_agent_register.py
.venv/bin/python $R create-space --name kube-handrail-test          # -> AGENT_SPACE_ID
.venv/bin/python $R register-mcp --name kube-handrail --scope "$SCOPE"  # -> SERVICE_ID
.venv/bin/python $R associate-read                                  # -> ASSOCIATION_ID
.venv/bin/python $R enable-write
.venv/bin/python $R install-guidance
```

Export each printed ID before the next step. `associate-read` adds the read tools as `READ_ONLY`; check in chat that the agent can read `web` before `enable-write`, which adds the apply tools as `MUTATIVE` and verifies the classification. `install-guidance` installs [`AGENTS.md`](../AGENTS.md) and the skills. Then set `devops_agent_space_id` and `devops_agent_association_id` in `terraform.tfvars` and apply again, so the [drift sentinel](../mcp/contract/sentinel.md) watches the association.

Start a **new** chat (a chat keeps the tool list it started with) and ask the agent to scale `web` to 2. DevOps Agent asks for your approval of the apply, pinned to its arguments.

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

It removes the guidance, the association and registration, runs `terraform destroy` and deletes `k8s/sandbox` and `build/k8s` from the cluster, asking before each step unless `--yes`.
