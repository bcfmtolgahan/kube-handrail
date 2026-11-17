# Deploying to a new account

What a deployment needs from the account, the cluster and the network, and the order of the steps. The commands are in the [quickstart](quickstart.md); the runtime calls are in [Network](../mcp/contract/admission-network.md#network) and the IAM permissions in [IAM roles and RBAC](../mcp/contract/identity.md#iam-roles-and-rbac).

## Prerequisites

- **Cluster.** An EKS cluster whose authentication mode is `API` or `API_AND_CONFIG_MAP`: the Lambdas get cluster access through access entries. Terraform stops before creating anything if the mode is `CONFIG_MAP`. Switching to `API_AND_CONFIG_MAP` keeps `aws-auth` working:

  ```sh
  aws eks update-cluster-config --name <cluster> --access-config authenticationMode=API_AND_CONFIG_MAP
  ```

  The admission policy needs Kubernetes 1.30 or later.
- **Region.** Amazon Bedrock AgentCore Gateway and AWS DevOps Agent must be available in the cluster's Region.
- **Operator credentials.** `eks:DescribeCluster`, and rights to create IAM roles and policies, Lambda functions and Function URLs, a DynamoDB table, SSM parameters, SNS topics, a Cognito user pool, an AgentCore gateway, EventBridge rules, CloudWatch log groups, alarms and a dashboard, an S3 bucket and SQS queue (audit), and EKS access entries. With the network options below, also security groups, their rules, VPC endpoints, and `ec2:DescribeSubnets`, `ec2:DescribeVpcs`, `ec2:DescribeVpcAttribute`, `ec2:DescribeRouteTables`, `ec2:DescribeVpcEndpointServices`, `ec2:DescribeSecurityGroups` and `ec2:GetSecurityGroupsForVpc`; destroying VPC Lambdas also needs `ec2:DescribeNetworkInterfaces`, `ec2:DetachNetworkInterface` and `ec2:DeleteNetworkInterface`. Applying `build/k8s` needs a cluster-admin kubeconfig that reaches the API server (for a private endpoint, from inside the VPC or over a VPN).
- **Build machine.** Terraform 1.5 or later (the AWS provider is pinned in `terraform/versions.tf` and `terraform/.terraform.lock.hcl`), `/bin/bash`, and a Python with `pip` (the `python` variable, default `python3`). The build runs `pip install --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12`, so the interpreter's version does not matter, but `terraform apply` needs PyPI.

## Names

Every AWS resource name starts with `name_prefix` (default `kube-handrail`): IAM roles and policies, Lambda functions and log groups, the plan table, the kill switches (`/<prefix>/remediation-enabled`, `/<prefix>/agents/<agent>/enabled`), the freezes parameter, SNS topics, the Cognito user pool, the gateway, rules, alarms, the audit bucket and the security groups.

- IAM names are global to the account: two deployments in one account need different prefixes, also in different Regions.
- The Cognito domain is `<prefix>-<account ID>`. Cognito does not allow `aws`, `amazon` or `cognito` in a domain, so Terraform rejects such a prefix.
- A log group left over from an earlier deployment with the same prefix makes the apply fail with "already exists". Delete or import it.

Not derived from `name_prefix`: the Kubernetes groups `kube-handrail-reader` and `kube-handrail-writer`, and the cluster objects in `build/k8s` (the Roles of those names, the ClusterRole `kube-handrail-policy-reader`, the admission policies `kube-handrail-writer-bounds` and `kube-handrail-writer-hpa-bounds` and their bindings). A cluster therefore holds one kube-handrail deployment. The test namespace is `kube-handrail-sandbox`.

## Namespaces, RBAC and admission policy

The tools act only in the namespaces of `namespaces`, each with its own bounds, actions, approvers and GitOps markers ([Namespace policy](../mcp/CONTRACT.md#namespace-policy)):

```hcl
namespaces = {
  "kube-handrail-sandbox" = { min_replicas = 0, max_replicas = 5 }
  "payments" = {
    min_replicas = 2
    max_replicas = 10
    actions      = ["adjust_hpa", "restart", "scale"]           # default: restart, rollback, scale
    approvers    = ["arn:aws:iam::<account>:role/payments-oncall"] # default: approver_principals
  }
}
```

`adjust_hpa` ([Adjust HPA](../mcp/contract/adjust-hpa.md)) is never a default: list it to give the writer `get` and `patch` on the namespace's HPAs. A namespace's `approvers` take effect only in `server` or `both` mode. The deprecated `sandbox_namespace`, `min_replicas` and `max_replicas` still define one namespace when `namespaces` is empty; Terraform warns while they are set and prints the equivalent entry.

`terraform apply` writes the cluster side to `build/k8s/` (also the `k8s_manifests` output): `rbac.yaml` (a reader Role in every namespace, a writer Role where an action is allowed), `policy-reader.yaml` (the ClusterRole that lets the reader `get` the two policies and their bindings) and `admission-policy.yaml`. Terraform does not talk to the cluster and creates no namespaces; they must exist first. Apply with cluster-admin rights:

```sh
kubectl apply --dry-run=server -f build/k8s/ && kubectl apply -f build/k8s/
$(terraform -chdir=terraform output -raw namespace_label_command)   # kube-handrail.io/managed=true
```

or `make upgrade UPGRADE_FLAGS=--with-k8s`. The label is an inventory marker for operators and `scripts/cleanup.sh`; the policy selects namespaces by name. `kubectl apply` never deletes: after removing a namespace or an action, delete the Roles no longer rendered (`--with-k8s` lists them). `k8s/sandbox/rbac.yaml` and `admission-policy.yaml` are superseded by `build/k8s`; do not apply them.

### Admission policy

The policy checks the replica bounds a second time, in the cluster, for the writer group only ([Admission policy](../mcp/contract/admission-network.md#admission-policy)).

- **Mode.** `admission_mode` (default `Deny`) is rendered into the binding, so re-applying `build/k8s` never turns enforcement off; change it in Terraform, not with `kubectl patch`.
- **Plan-time check.** Every plan reads the policy and binding and is refused with `conflict` (`details.check = "admission_policy_match"`) unless they are in `Deny`, select the namespace, hold Terraform's bounds and are otherwise as rendered. It fails closed. So after an apply that changes bounds or namespaces, plans are refused until `build/k8s` is applied.
- **Trial.** To watch the policy first, set `admission_mode = "Warn"` and `admission_check = false`, apply both sides, and check that `kubectl get validatingadmissionpolicy kube-handrail-writer-bounds -o jsonpath='{.status.typeChecking}'` shows no `expressionWarnings`. `tests/admission/run.sh` runs server-side dry runs as the writer and reader groups (`NAMESPACE`, `MIN_REPLICAS`, `MAX_REPLICAS`, and the `_2` variables for a second namespace; leave `OTHER_NAMESPACE` unset on a shared cluster). Then set `Deny` and `admission_check = true` and apply both sides again.
- **Older clusters.** Below 1.30, set `admission_check = false` and apply only `build/k8s/rbac.yaml`.

## Agents

Each agent is one OAuth client ([Agent identity](../mcp/contract/identity.md)). `agents` maps names (`^[a-z][a-z0-9_]{0,31}$`) to `{client_id = optional(string), hourly_apply_budget = optional(number)}`. An entry without `client_id` gets a Cognito app client; with one, that existing client of the user pool is allowed. The default, `{devops_agent = {}, smoke_test = { hourly_apply_budget = 30 }}`, gives the tests enough budget for a smoke test followed by the end-to-end suite; keep that if you set `agents` yourself. The gateway allows exactly these clients, and the drift sentinel reports any other.

- **Add an agent.** Add a key, apply, read its client ID from `agent_client_ids` and its secret from Cognito (`aws cognito-idp describe-user-pool-client`), and give both only to that agent. Register it in DevOps Agent with `register-mcp`. Two agents sharing a client are one agent to the server.
- **Budgets.** `hourly_apply_budget`, else `agent_hourly_apply_budget` (10), per clock hour; `global_hourly_apply_budget` caps all agents together.
- **Stop or restore one agent.** `agent_kill_switch_parameters` names each switch; Terraform creates it as `true` and ignores later changes. The global switch still stops everyone, and the sentinel never writes these.

  ```sh
  aws ssm put-parameter --overwrite --name /<prefix>/agents/<agent>/enabled --value false   # stop
  aws ssm put-parameter --overwrite --name /<prefix>/agents/<agent>/enabled --value true    # restore
  ```

- **Rotate the caller key.** `terraform apply -replace=terraform_data.caller_key`. Writes in the moment the Lambdas disagree are refused with `agent_unknown`.

## Approval mode

| Mode | Use it for | Who approved, in the record |
|---|---|---|
| `server` (default) | Production without DevOps Agent chat approvals, and any deployment that needs an audit-grade approver | The approver's IAM identity, checked by the approval API and pinned to the plan's exact arguments |
| `both` | Production with DevOps Agent: the chat approval stays the agent's flow, the server approval is the record | As `server` |
| `client` | Evaluation and test deployments | Nobody, reliably |

In `client` mode the only approval is the chat's, and it cannot be bound to a plan: CloudTrail records `UpdateApprovalAction` with the approver's identity but redacts every tool argument, the plan ID included, so it can only be matched by time. `server` and `both` record the approver on the plan in the same conditional write that allows the apply. `scripts/audit.py verify` reports a `client`-mode apply as `client_unobserved`, with the window to search CloudTrail.

Approvers are IAM users or roles in `approver_principals` (or a namespace's `approvers`), written as `aws sts get-caller-identity` names them; a role ARN covers its sessions. With `approval_require_mfa = true` (default) attach the `approver_policy_arn` output to each; `false` grants them in the function's resource policy without MFA, for test setups only. `approval_email` subscribes an address to approval requests. A mode change takes effect at the Lambdas' next cold start.

### Shadow mode

To watch what the agent would do before it changes anything, deploy in `server` mode and let no approver act. Every plan, with its diff, impact and warnings, lands in the plan table, the audit record and the approval topic, and expires unapplied; the write Lambda refuses every apply without a server approval.

1. Set `approval_mode = "server"` and apply. For a hard guarantee, set `approver_principals` to a role nobody assumes; to let reviewers use `scripts/approve.py show`, list reviewers who agree never to approve.
2. Subscribe reviewers (`approval_email`) and read plans with `scripts/audit.py export` or `scripts/approve.py show`.
3. Compare what the agent planned with what the on-call engineer did.
4. To go live, set the real approvers, apply, and attach `approver_policy_arn`. No tool changes, so no `enable-write`.

### Dashboard

With `sentinel_enabled`, Terraform creates a CloudWatch dashboard named after `name_prefix`: plan and apply calls, guard rejections by code, tool errors, sentinel results, the verifier's runs, backlog, degraded changes and reverts, and the audit pipeline's failures and lag. It reads only metrics the Lambdas already emit ([Observability](../mcp/contract/observability.md)).

## Audit record retention

Every change to a plan record is written once to the audit bucket, under the default Object Lock retention `audit_object_lock_mode` for `audit_retention_days` ([Bucket](../mcp/contract/audit.md#bucket)).

- **Production: `COMPLIANCE`.** Under `GOVERNANCE`, the default, any principal allowed `s3:BypassGovernanceRetention` can delete entries or shorten their retention. Under `COMPLIANCE` nobody can, the root user included, until each entry's retention ends.
- **`COMPLIANCE` cannot be taken back.** Entries written under it keep their retention; switching back affects only later entries, and a retention can be extended but never shortened. The switch is one in-place update of `aws_s3_bucket_object_lock_configuration.audit[0]`. Try it on a test deployment first.
- **Destroying.** Under `GOVERNANCE`, `make destroy` (`scripts/cleanup.sh`) or `audit_force_destroy = true` empties the bucket (both need `s3:BypassGovernanceRetention`). Under `COMPLIANCE`, `destroy` removes everything else and fails on the bucket until the newest entry's retention ends. To finish, `terraform state rm 'aws_s3_bucket.audit[0]'` and each remaining `aws_s3_bucket_*.audit[0]` (no wildcards), and delete the bucket later; a new deployment with the same prefix cannot create its bucket until then. Keep `GOVERNANCE` on test deployments.

## Network

The tool Lambdas call the cluster's API server, `eks:DescribeCluster`, DynamoDB, SSM and SNS, all over HTTPS; never STS or CloudWatch Logs. The other Lambdas never talk to the cluster and always run outside any VPC.

| Cluster endpoint | Setting |
|---|---|
| Public, open to `0.0.0.0/0` | Nothing to set. The Lambdas run outside any VPC. |
| Public, limited to `public_access_cidrs`, with private access | Set `lambda_subnet_ids`; the Lambdas then use the private endpoint (Terraform warns otherwise). |
| Public, limited, without private access | Turn on private access and set `lambda_subnet_ids`, or use subnets with a NAT gateway whose address is in `public_access_cidrs` (Terraform warns). |
| Private only | `lambda_subnet_ids` is required; Terraform stops without it. |

With `lambda_subnet_ids`:

1. **Subnets.** Private subnets in the cluster's VPC, preferably in several Availability Zones. Subnets in another VPC need routing and DNS to the private endpoint, which this module does not set up.
2. **DNS.** The private endpoint resolves privately only with `enableDnsSupport` and `enableDnsHostnames` on.
3. **Security groups.** Without `lambda_security_group_ids`, Terraform creates `<prefix>-lambda` (outbound TCP 443 only). The API server must accept TCP 443 from it: `allow_lambda_to_cluster_endpoint = true` adds that rule to the cluster security group (off by default, because it changes a cluster security group; needs private access), or add it yourself.
4. **AWS APIs.** A NAT gateway, or `create_vpc_endpoints = true`: interface endpoints with private DNS for `eks`, `ssm` and `sns` (one subnet per Availability Zone; their security group accepts TCP 443 from the whole VPC, because private DNS sends every client there) and a DynamoDB gateway endpoint on the Lambda subnets' route tables. It needs private endpoint access. Leave it off if the VPC already has any of these endpoints: a duplicate fails to create, and the existing ones must accept the Lambdas and allow the calls in the contract.

A missing piece shows as a 30-second Lambda timeout, not an error envelope; check the Lambda's log group.

On `destroy`, Lambda releases the network interfaces of VPC functions only some time after deleting them. The interface permissions are a separate role policy that Terraform removes after the `<prefix>-lambda` group, and deleting the group can wait many minutes. With your own `lambda_security_group_ids`, interfaces can be left behind: delete them and run `destroy` again.

## Order of steps

1. **Namespaces.** Create them; for a test, the sandbox ([quickstart](quickstart.md#1-sandbox-namespace)).
2. **Variables.** `terraform/terraform.tfvars` with at least `cluster_name`, `region`, `namespaces`, the network variables for a private endpoint, and `agents` if more than DevOps Agent and the tests call the gateway. Use `approval_mode = "client"` for the first tests.
3. **Infrastructure.** `terraform init`, `plan`, `apply`, then apply `build/k8s` and run the label command ([quickstart](quickstart.md#2-infrastructure)). The plan fails early if the authentication mode, endpoint access and network variables do not fit.
4. **Smoke and guard tests** ([quickstart](quickstart.md#3-smoke-test), [Testing](milestone-0.md#guard-tests)).
5. **DevOps Agent:** `create-space`, `register-mcp`, `associate-read`, a check in chat, `enable-write`, `install-guidance` ([quickstart](quickstart.md#4-register-with-aws-devops-agent)).
6. **Sentinel.** Set `devops_agent_space_id` and `devops_agent_association_id` and apply again.
7. **Server-side approval.** Set `approval_mode` and the approvers, apply, attach `approver_policy_arn` ([quickstart](quickstart.md#5-server-side-approval)).
8. **Subscriptions.** SNS email subscriptions stay pending until confirmed.

## Checks before anything is created

Terraform stops at plan time when the authentication mode is not `API` or `API_AND_CONFIG_MAP`; the cluster has no public endpoint and `lambda_subnet_ids` is empty; `lambda_security_group_ids`, `allow_lambda_to_cluster_endpoint` or `create_vpc_endpoints` is set without `lambda_subnet_ids`; one of the last two is set without private endpoint access; `create_vpc_endpoints` is set and the VPC lacks DNS support or hostnames; `name_prefix` contains `aws`, `amazon` or `cognito`; or another check fails (bounds, approvers, TTLs, tool names, classification). It warns when the public endpoint is limited and the Lambdas are outside a VPC, when the subnets are in another VPC, when they are set without private endpoint access, and when a private-endpoint cluster's VPC lacks DNS support or hostnames.

## Upgrading

Run `make upgrade` (`scripts/upgrade.py`) from a checkout of the new release, with the same `terraform.tfvars` (or `UPGRADE_FLAGS=--var-file=...`) and the deployment's state ([`terraform/backend.tf.example`](../terraform/backend.tf.example) keeps it in S3). First `make upgrade UPGRADE_FLAGS=--dry-run`, which plans, checks and changes nothing. Then `make upgrade`:

1. refuses to start while the global kill switch is off, unless it turned it off in the run it resumes;
2. plans and stops if the plan deletes or replaces the gateway, a gateway target, the Cognito user pool, domain or a client, the plan table, the audit bucket, the kill switch or the freezes parameter, or replaces an agent's kill switch; it shows the plan and asks (`--yes` skips);
3. turns the kill switch off when the tool set changes (`tools.json` or `classification.json` differ from the deployed `tool_set_hash`);
4. applies a known two-step migration with `-target` first, then the plan, and retries once on "inconsistent final plan". With `--with-k8s`, it writes `build/k8s`, dry-runs and applies it with the current kubectl context (or `--kube-context`), checks both bindings' mode and lists Roles no longer rendered; `--k8s-only` runs only this step;
5. runs `enable-write` when the tool set changed, requires a sentinel `ok`, and turns the kill switch back on only if it turned it off and nothing else wrote it since;
6. runs `install-guidance` when `AGENTS.md` or a skill changed (open chats may keep the old guidance for about half an hour);
7. runs the smoke test, `--read-only` in `server` and `both` mode.

`enable-write` and `install-guidance` need the DevOps Agent IDs (tfvars, `AGENT_SPACE_ID` and `ASSOCIATION_ID`, or flags); without them they are skipped with a warning. The smoke test uses the `smoke_test` client and reads its secret from Cognito (or `CLIENT_ID` and `CLIENT_SECRET`). Progress is kept in `.upgrade-state.json`; after a failure, fix the cause and run `make upgrade UPGRADE_FLAGS=--resume`. Until the apply is done, a resume plans again and turns the kill switch off again if the new plan changes the tool set, also after the switch was turned on by hand. In `client` mode a second upgrade within `flap_window_seconds` fails the smoke test with `flapping`: wait, or pass `--skip-smoke`.

What changes in each release, and anything to do by hand, is in [CHANGELOG.md](../CHANGELOG.md) and the [upgrade notes](history/upgrading.md); the release rules are in [Releasing](releasing.md).
