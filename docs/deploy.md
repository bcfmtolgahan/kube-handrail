# Deploying to a new account

What a deployment needs from the account, the cluster and the network, and the order of the steps. The commands for each step, and what the tests check, are in [Milestone 0](milestone-0.md). The runtime calls and IAM permissions are in the [contract](../mcp/CONTRACT.md#network).

Status: implemented and tested. A fresh deployment with its own prefix and state ran against a test cluster with only a private endpoint, with the Lambdas in subnets without a route out of the VPC and `create_vpc_endpoints` in a VPC shared with other workloads; `lambda_subnet_ids` with a NAT gateway and moving the Lambdas into and out of a VPC were tested on another deployment. A brand-new AWS account has not been used: the fresh deployment shared its account with another one.

## Prerequisites

- **Cluster.** An existing EKS cluster whose authentication mode is `API` or `API_AND_CONFIG_MAP`, because the Lambdas get cluster access through access entries. Terraform reads the cluster and stops before creating anything if the mode is `CONFIG_MAP`. Switching the mode to `API_AND_CONFIG_MAP` keeps the `aws-auth` ConfigMap working:

  ```sh
  aws eks update-cluster-config --name <cluster> --access-config authenticationMode=API_AND_CONFIG_MAP
  ```

- **Region.** Amazon Bedrock AgentCore Gateway and AWS DevOps Agent must be available in the Region of the cluster.
- **Operator credentials.** Rights to read the cluster (`eks:DescribeCluster`) and to create IAM roles and policies, Lambda functions and Function URLs, a DynamoDB table, an SSM parameter, SNS topics, a Cognito user pool, an AgentCore gateway, EventBridge rules, CloudWatch log groups, alarms and a dashboard, and EKS access entries; with the network options below, also security groups, security group rules and VPC endpoints, and the EC2 read actions `ec2:DescribeSubnets`, `ec2:DescribeVpcs`, `ec2:DescribeVpcAttribute`, `ec2:DescribeRouteTables`, `ec2:DescribeVpcEndpointServices`, `ec2:DescribeSecurityGroups` and `ec2:GetSecurityGroupsForVpc` (Lambda checks the last two on the caller that attaches a VPC). Destroying VPC Lambdas also needs `ec2:DescribeNetworkInterfaces`, `ec2:DetachNetworkInterface` and `ec2:DeleteNetworkInterface` (below). Applying the sandbox RBAC needs a kubeconfig with cluster-admin rights that can reach the cluster's API server; for a private endpoint that means from inside the VPC or over a VPN.
- **Build machine.** Terraform 1.5 or later (the AWS provider is pinned to `~> 6.70` in `terraform/versions.tf`, and `terraform/.terraform.lock.hcl` pins the exact versions), `/bin/bash`, and a Python with `pip` given by the `python` variable (default `python3`). The build runs `pip install --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12`, so the interpreter's own version does not matter, but the machine needs access to PyPI during `terraform apply`.

## Names

Every AWS resource name starts with `name_prefix` (default `kube-handrail`): IAM roles and the approver policy (`<prefix>-k8s-read`, `-k8s-write`, `-gateway`, `-approver`, `-approver-invoke`, `-sentinel`, `-interceptor`, `-verifier`), Lambda functions and their log groups (`/aws/lambda/<prefix>-...`), the plan table (`<prefix>-plans`), the kill switch (`/<prefix>/remediation-enabled`) and the per-agent kill switches (`/<prefix>/agents/<agent>/enabled`), SNS topics (`<prefix>-approvals`, `<prefix>-alerts`), the Cognito user pool and resource server, the gateway, the sentinel and verifier rules and alarms, and the security groups (`<prefix>-lambda`, `<prefix>-vpc-endpoints`).

- IAM names are global to the account. Two deployments in one account need different prefixes, also when they are in different Regions.
- The Cognito domain is `<prefix>-<account ID>`, so it does not collide with other accounts. Cognito does not allow `aws`, `amazon` or `cognito` in a domain, so the prefix may not contain them either; Terraform rejects such a prefix.
- A log group left over from an earlier deployment with the same prefix makes the apply fail with "already exists". Delete it, or import it into the state.

Not derived from `name_prefix`:

- The Kubernetes groups `kube-handrail-reader` and `kube-handrail-writer`, which the access entries map the Lambda roles to. Deployments on the same cluster share them, and with them the RoleBindings.
- The cluster objects in `build/k8s`: the Roles `kube-handrail-reader` and `kube-handrail-writer`, the ClusterRole `kube-handrail-policy-reader` and the admission policy `kube-handrail-writer-bounds`. A cluster holds one copy, so it holds one kube-handrail deployment.
- The test namespace in `k8s/sandbox/namespace.yaml` and in `tests/e2e/e2e_gateway.py`, `kube-handrail-sandbox`. `scripts/smoke_test.py` takes `NAMESPACE`.

## Namespaces, RBAC and admission policy

Status: implemented, not yet tested on a cluster. The rendered manifests are checked against golden files, and the admission policy for two namespaces on kind, in CI.

The tools act only in the namespaces of `namespaces`, each with its own replica bounds, allowed actions, approvers and GitOps markers ([Namespace policy](../mcp/CONTRACT.md#namespace-policy)):

```hcl
namespaces = {
  "kube-handrail-sandbox" = { min_replicas = 0, max_replicas = 5 }
  "payments" = {
    min_replicas = 2
    max_replicas = 10
    actions      = ["restart", "scale"]                             # default: restart, rollback, scale
    approvers    = ["arn:aws:iam::111122223333:role/payments-oncall"] # default: approver_principals
  }
}
```

A namespace's `approvers` take effect only with `approval_mode` `server` or `both`; in `client` mode Terraform warns that they are unused.

Without `namespaces`, the deprecated `sandbox_namespace`, `min_replicas` and `max_replicas` still define one namespace, as before; Terraform warns while they are set and refuses them together with `namespaces`. To move, write their values into `namespaces` (the warning prints the entry) and remove them; the rendered policy stays the same.

`terraform apply` writes the cluster side to `build/k8s/` (also the `k8s_manifests` output): `rbac.yaml` (in every namespace a reader Role, and a writer Role where an action is allowed, bound to the access entries' groups, whose names must stay as they are), `policy-reader.yaml` (the ClusterRole `kube-handrail-policy-reader`: `get` on the admission policy and its binding only, the reader's one cluster-scoped right) and `admission-policy.yaml`. Terraform does not talk to the cluster and creates no namespaces. With a kubeconfig that may create these objects (cluster-admin):

```sh
kubectl apply --dry-run=server -f build/k8s/ && kubectl apply -f build/k8s/
$(terraform -chdir=terraform output -raw namespace_label_command)   # kube-handrail.io/managed=true
```

or `make upgrade UPGRADE_FLAGS=--with-k8s`, which does the same after the Terraform apply. The namespaces must exist first. The label is an inventory marker for operators and `scripts/cleanup.sh`; the policy and the bindings select namespaces by name, so removing it weakens nothing. `kubectl apply` never deletes: after removing a namespace or narrowing its actions, delete the Roles that are no longer rendered (`make upgrade UPGRADE_FLAGS=--with-k8s` lists them).

For the test namespace, `kubectl apply -f k8s/sandbox/namespace.yaml -f k8s/sandbox/web-deployment.yaml` creates it with the `web` Deployment ([Milestone 0, step 1](milestone-0.md#1-sandbox-namespace-rbac-and-target-workload)). `k8s/sandbox/rbac.yaml` and `admission-policy.yaml` are superseded by `build/k8s`; do not apply them.

### Admission policy

The policy is a second check of the replica bounds in the cluster, for the writer group in the policy's namespaces only ([Admission policy](../mcp/CONTRACT.md#admission-policy)). It needs **Kubernetes 1.30 or later**.

- **Mode.** `admission_mode` (default `Deny`) is rendered into the binding, so applying `build/k8s` again never turns enforcement off silently; change it in Terraform, not with `kubectl patch`.
- **Plan-time check.** Before every plan the read Lambda reads the policy and the binding and refuses the plan with `conflict` (`details.check = "admission_policy_match"`, `details.reason` saying what) unless the binding is in `Deny`, both select the namespace, the bounds equal Terraform's and the enforcing parts are as rendered (a policy switched off by hand with its bounds intact is `policy_modified` or `binding_modified`). It fails closed: if it cannot read them (no ClusterRole, an API error), plans are refused too. So after an apply that changes bounds or namespaces, plans are refused until `build/k8s` is applied, and a binding switched to `Warn` by hand stops all plans.
- **Trial.** To watch the policy before enforcing it, set `admission_mode = "Warn"` and `admission_check = false` (Terraform warns about `Warn` with the check on), apply both sides, and check that `kubectl get validatingadmissionpolicy kube-handrail-writer-bounds -o jsonpath='{.status.typeChecking}'` shows no `expressionWarnings`. `tests/admission/run.sh` impersonates the writer and reader groups with server-side dry runs (cluster-admin; `NAMESPACE`, `MIN_REPLICAS`, `MAX_REPLICAS`, and `NAMESPACE_2`, `MIN_REPLICAS_2`, `MAX_REPLICAS_2` for a second namespace); leave `OTHER_NAMESPACE` unset on a shared cluster, where it would create and delete a namespace. Then set `Deny` and `admission_check = true` and apply both sides again.
- **Older clusters.** Below 1.30, set `admission_check = false` and apply only `build/k8s/rbac.yaml`.

`scripts/cleanup.sh` deletes the files in `build/k8s` from the cluster and removes the label from the namespaces they name; the namespaces stay.

## Agents

Each agent is one OAuth client ([Agent identity](../mcp/CONTRACT.md#agent-identity)). The `agents` variable maps agent names (`^[a-z][a-z0-9_]{0,31}$`) to `{client_id = optional(string), hourly_apply_budget = optional(number)}`. An entry without `client_id` gets a Cognito app client with the `read` and `remediate` scopes; with `client_id`, that existing client of the user pool is allowed instead. The default is `{devops_agent = {}, smoke_test = { hourly_apply_budget = 30 }}`: one client for DevOps Agent and one for the smoke and end-to-end tests, whose runs apply more than the default budget of 10 in an hour. If you set `agents` yourself, keep that budget for `smoke_test`, or the smoke test followed by the end-to-end suite is refused with `agent_budget_exceeded`. The gateway allows exactly the agents' clients, and the drift sentinel reports any other.

- **Add an agent.** Add a key, apply, read the new client's ID from the `agent_client_ids` output and its secret from Cognito (`aws cognito-idp describe-user-pool-client`), and give both only to that agent. To register it in DevOps Agent, run `scripts/devops_agent_register.py register-mcp` with its `CLIENT_ID` and `CLIENT_SECRET`. Give each agent its own client: two agents sharing one are one agent to the server.
- **Budgets.** `hourly_apply_budget` on an entry, otherwise `agent_hourly_apply_budget` (default 10), is the agent's apply budget per clock hour; `global_hourly_apply_budget` still caps all agents together.
- **Stop or restore one agent.** The `agent_kill_switch_parameters` output names each agent's switch. Terraform creates it as `true` and ignores later changes:

  ```sh
  aws ssm put-parameter --overwrite --name /<prefix>/agents/<agent>/enabled --value false   # stop
  aws ssm put-parameter --overwrite --name /<prefix>/agents/<agent>/enabled --value true    # restore
  ```

  The global kill switch still stops every agent. The drift sentinel never writes the agent switches.
- **Rotate the caller key.** `terraform apply -replace=terraform_data.caller_key` generates a new `CALLER_KEY` and sets it on the interceptor and both tool Lambdas. Writes in the moment they disagree are refused with `agent_unknown`.

The end-to-end identity cases (`tests/e2e/e2e_gateway.py --with-identity`) need a second agent that only the test uses, with a budget the test can exhaust, and enough budget for the smoke test agent to run the whole suite:

```hcl
agents = {
  devops_agent = {}
  smoke_test   = { hourly_apply_budget = 30 } # at least the suite's applies
  e2e_peer     = { hourly_apply_budget = 2 }  # used only by --with-identity
}
```

Then, next to the variables the end-to-end tests already need, set `CLIENT_ID_B` to `agent_client_ids["e2e_peer"]` and `CLIENT_SECRET_B` to that client's secret. `AGENT_NAME` (default `smoke_test`, the agent of `CLIENT_ID`), `AGENT_NAME_B` (default `e2e_peer`) and `AGENT_BUDGET_B` (default 2) must match the variables when they differ. The test derives the agent switch of `e2e_peer` from `KILL_SWITCH_PARAM` and turns it off and on again, so the operator's credentials need `ssm:PutParameter` on it. Budgets count per clock hour: a server-mode run right after a client-mode run waits for the next hour if `e2e_peer`'s budget is spent.

The end-to-end tests do not remove the interceptor from the gateway. That case is a manual live test: with the interceptor detached, a plan whose `_kube_handrail_caller` the client set itself, to its own or another agent's `client_id`, must be refused with `forbidden`, `agent_unknown`, and the next sentinel run must report `missing_interceptor`.

## Approval mode

Recommendation (`approval_mode`):

| Mode | Use it for | Who approved, in the record |
|---|---|---|
| `server` (default) | Production without DevOps Agent chat approvals, and any deployment that needs an audit-grade approver | The approver's IAM identity, checked by the approval API and pinned to the plan's exact arguments |
| `both` | Production with DevOps Agent: the chat approval stays the agent's UX, and the server approval is the record | As `server` |
| `client` | Evaluation and convenience: trying kube-handrail out, test deployments | Nobody, reliably: the approver is best-effort |

In `client` mode the only approval is the one in the DevOps Agent chat, and kube-handrail cannot bind it to a plan. AWS CloudTrail records the chat approval (`UpdateApprovalAction`) as a management event with the approver's IAM identity, but every tool argument in it is redacted, the plan ID included, and its request parameters hold no approval ID. An approval can therefore only be matched to a plan by time, which does not hold up when two plans are approved close together. `server` and `both` record the approver on the plan itself, in the same conditional write that makes the apply possible ([Server-side approval](../mcp/CONTRACT.md#server-side-approval)). Each plan records its `approval_mode`, and `scripts/audit.py verify` reports a `client`-mode apply as `client_unobserved`, with the window in which to look for the chat approval in CloudTrail (implemented, not yet tested on a cluster).

### Shadow mode

To watch what the agent would do before letting it change anything, deploy in `server` mode and let no approver act. The agent investigates, plans and calls `request_approval` as usual; every plan, with its diff, impact preview and warnings, lands in the plan table, the audit record and the approval topic, and expires unapplied. Nothing reaches the cluster: in `server` mode the write Lambda refuses every apply without a server approval (`approval_required`).

1. Set `approval_mode = "server"` and apply. `approver_principals` needs one entry in `server` mode: for a hard guarantee, a role nobody assumes during the trial; to let reviewers use `scripts/approve.py show`, the reviewers, who agree never to run `approve`.
2. Subscribe reviewers to the approval topic (`approval_email`), and read plans with `scripts/audit.py export <plan_id>` or `scripts/approve.py show <plan_id>`. The dashboard (below) counts plans and refusals.
3. Compare what the agent planned with what the on-call engineer did.
4. To go live, set `approver_principals` to the real approvers, apply, and attach `approver_policy_arn` to each. No tool or registration changes, so `enable-write` is not needed.

Leave the kill switch on: it only matters for applies, which `server` mode refuses without an approval anyway. This is a procedure, not a mode in the code.

### Dashboard

With `sentinel_enabled` (like the alarms), Terraform creates a CloudWatch dashboard named after `name_prefix`. It shows plan and apply calls per tool, guard rejections by code, tool errors, the sentinel's drift and unknown results and, when enabled, the verifier's runs, backlog, degraded changes and reverts and the audit record's dead letters, write failures and stream lag. It reads only the metrics the Lambdas already emit ([Observability](../mcp/CONTRACT.md#observability)). Tested on a test cluster: the dashboard is created and its body is accepted.

## Audit record retention

Every change to a plan record is written once to the audit bucket, under the default Object Lock retention `audit_object_lock_mode` for `audit_retention_days` ([Bucket](../mcp/CONTRACT.md#bucket)).

- **Production: `COMPLIANCE`.** Set `audit_object_lock_mode = "COMPLIANCE"` and `audit_retention_days` to what your retention policy requires. Under `GOVERNANCE`, the default, any principal allowed `s3:BypassGovernanceRetention` (an account administrator, for example) can delete entries or shorten their retention, so the record holds only against the roles kube-handrail creates. Under `COMPLIANCE` nobody can, the account's root user included, until each entry's retention ends.
- **`COMPLIANCE` cannot be taken back.** An entry written under it keeps its retention: switching back to `GOVERNANCE` changes only entries written afterwards, and a retention can be extended but never shortened. A mistyped `audit_retention_days` is paid for in storage for that long. On an existing deployment the switch is one in-place update of `aws_s3_bucket_object_lock_configuration.audit[0]`, and earlier entries keep `GOVERNANCE`. Try it on a test deployment first.
- **Destroying a deployment.** `terraform destroy` cannot delete a bucket that still holds entries. Under `GOVERNANCE`, `make destroy` (`scripts/cleanup.sh`) empties it first, and `audit_force_destroy = true` lets Terraform empty it; both need `s3:BypassGovernanceRetention`. Under `COMPLIANCE` neither works before the newest entry's retention ends: `destroy` removes everything else and fails on the bucket, which stays in the account and in the state. Resources that depend on the bucket, such as its policy, public access block and encryption configuration, may already be destroyed by then: check the leftover bucket's settings. To finish, remove what is left from the state by its address (`terraform state list | grep audit`, then `terraform state rm 'aws_s3_bucket.audit[0]'` and each remaining `aws_s3_bucket_*.audit[0]`; `state rm` takes no wildcards), delete the bucket once the retention has passed, and expect a new deployment with the same `name_prefix` in that account to fail to create its bucket until then. Keep `GOVERNANCE` on test and CI deployments that are destroyed.

## Network

The tool Lambdas (`k8s_read` and `k8s_write`) call the cluster's API server, `eks:DescribeCluster`, DynamoDB, SSM (the kill switches) and SNS (read), all over HTTPS. They never call STS or CloudWatch Logs. The approver, sentinel, interceptor and verifier Lambdas do not talk to the cluster and always run outside any VPC; a verification runs inside the write Lambda. Details are in the [contract](../mcp/CONTRACT.md#network).

| Cluster endpoint | Setting |
|---|---|
| Public, open to `0.0.0.0/0` (with or without private access) | Nothing to set. The Lambdas run outside any VPC. |
| Public, limited to `public_access_cidrs`, with private access | Terraform warns: Lambdas outside a VPC have no fixed source address. Set `lambda_subnet_ids`; the Lambdas then use the private endpoint. |
| Public, limited to `public_access_cidrs`, without private access | Lambdas outside a VPC have no fixed source address. Either turn on private endpoint access and set `lambda_subnet_ids`, or set `lambda_subnet_ids` to subnets with a NAT gateway whose address is in `public_access_cidrs`; Terraform warns about the latter. `allow_lambda_to_cluster_endpoint` and `create_vpc_endpoints` need private access, and Terraform refuses them without it. |
| Private only | `lambda_subnet_ids` is required; Terraform stops without it. |

With `lambda_subnet_ids` set:

1. **Subnets.** Use private subnets in the cluster's VPC, preferably in more than one Availability Zone. Terraform warns if they are in another VPC; the private endpoint must then be routed and resolvable from there (peering or Transit Gateway, plus DNS), which this module does not set up.
2. **DNS.** The private endpoint's name resolves to private addresses only inside a VPC with `enableDnsSupport` and `enableDnsHostnames` on. Terraform warns if either is off, and refuses to create the VPC endpoints without them.
3. **Security groups.** Without `lambda_security_group_ids`, Terraform creates `<prefix>-lambda`, which allows outbound TCP 443 only. The API server must accept TCP 443 from that group. `allow_lambda_to_cluster_endpoint = true` adds the rule to the cluster security group (the one EKS created, `vpc_config.cluster_security_group_id`); it is off by default because it changes a security group of the cluster, and it needs private endpoint access: the cluster security group does not filter the public endpoint. Otherwise add the rule yourself, to the cluster security group or to an additional security group of the cluster; the `lambda_security_group_ids` output lists the source groups.
4. **AWS APIs.** The subnets need a route to DynamoDB, EKS, SSM and SNS in the Region: either a NAT gateway, or VPC endpoints. `create_vpc_endpoints = true` creates interface endpoints with private DNS for `eks`, `ssm` and `sns` (one subnet per Availability Zone from `lambda_subnet_ids`, with a security group `<prefix>-vpc-endpoints` that accepts TCP 443 from the VPC's address ranges: private DNS sends every client in the VPC to these endpoints, so other workloads that call EKS, SSM or SNS keep working) and a DynamoDB gateway endpoint on the route tables of the Lambda subnets (the VPC's main route table for a subnet without an explicit association). It needs private endpoint access on the cluster: no VPC endpoint carries Kubernetes API traffic. Leave it off if the VPC already has endpoints for any of these services: a second interface endpoint with private DNS for the same service, or a second DynamoDB gateway endpoint on the same route table, fails to create. Existing endpoints must then accept TCP 443 from the Lambdas' security groups, and their endpoint policies must allow the calls in the contract. Interface endpoints are billed per hour and Availability Zone.

If any of these is missing, a tool call does not return an error envelope: the Lambda waits on the connection until its 30-second timeout and the call fails. Check the Lambda's log group for a timeout.

On `terraform destroy`, Lambda releases the network interfaces of VPC functions only some time after the functions are deleted, and needs the role's permissions to do so. The network interface permissions are therefore a separate role policy (`vpc-network-interfaces`) that Terraform removes only after the `<prefix>-lambda` security group, and before deleting that group the AWS provider deletes the network interfaces Lambda left behind, with the operator's credentials. Deleting the group can wait many minutes. When one apply moves the Lambdas out of a VPC, the functions are updated first, then the group and that policy are deleted (the group is `create_before_destroy` for this). With your own `lambda_security_group_ids`, the policy can go before Lambda has released the interfaces; Lambda then cannot delete the interfaces, and they remain until you delete them. If deleting the group fails on a dependency violation, find the network interfaces that still use the group, delete them, and run `destroy` again.

## Order of steps

1. **Namespaces.** Create the namespaces the tools will act in, for a test the sandbox (above).
2. **Variables.** Copy `terraform/terraform.tfvars.example` to `terraform/terraform.tfvars` and set at least `cluster_name`, `region`, `namespaces` and, for a private endpoint, the network variables. Adjust `agents` if more than DevOps Agent and the tests will call the gateway ([Agents](#agents)). For the first tests set `approval_mode = "client"`: the smoke test and the default guard tests apply through the gateway, which `server` mode refuses without an approval.
3. **Infrastructure.** `terraform -chdir=terraform init`, `plan`, `apply` ([Milestone 0, step 2](milestone-0.md#2-infrastructure)). The plan fails early if the authentication mode, the endpoint access or the network variables do not fit together. Then apply `build/k8s` and run the label command ([above](#namespaces-rbac-and-admission-policy)); the bindings name the groups, not the roles.
4. **Smoke and guard tests** through the gateway ([Milestone 0, step 3](milestone-0.md#3-smoke-test-through-the-gateway)). On a private-endpoint cluster, `kubectl` for the guard tests must reach the API server; the tool calls themselves go through the gateway and need no network access to the cluster from where they are run.
5. **Register with AWS DevOps Agent**: `create-space`, `register-mcp`, `associate-read`, check in chat, then `enable-write` ([Milestone 0, step 4](milestone-0.md#4-register-with-aws-devops-agent)).
6. **Let the sentinel watch the association.** Set `devops_agent_space_id` and `devops_agent_association_id` and apply again.
7. **Server-side approval.** Set `approval_mode = "server"` (or `both`) and `approver_principals` (and a namespace's own `approvers` where only they may decide its plans), apply, and attach the `approver_policy_arn` policy to each approver, the namespaces' approvers included ([Server-side approval](milestone-0.md#server-side-approval)). The tool set does not change, so the sentinel sees no drift.
8. **Subscriptions.** SNS email subscriptions (`alert_email`, `approval_email`) stay pending until the address confirms them.

## Checks before anything is created

Terraform stops at plan time when:

- the cluster's authentication mode is not `API` or `API_AND_CONFIG_MAP`;
- the cluster has no public endpoint and `lambda_subnet_ids` is empty;
- `lambda_security_group_ids`, `allow_lambda_to_cluster_endpoint` or `create_vpc_endpoints` is set without `lambda_subnet_ids`;
- `allow_lambda_to_cluster_endpoint` or `create_vpc_endpoints` is set and the cluster has no private endpoint access;
- `create_vpc_endpoints` is set and the VPC lacks DNS support or DNS hostnames;
- `name_prefix` contains `aws`, `amazon` or `cognito`, or any of the existing checks fails (replica bounds, approvers, TTLs, tool names, classification).

It warns, and continues, when the public endpoint is limited to `public_access_cidrs` and the Lambdas are outside a VPC, when `lambda_subnet_ids` are in another VPC than the cluster, when `lambda_subnet_ids` is set and the cluster has no private endpoint access (the Lambdas then need a NAT gateway whose address is in `public_access_cidrs`), and when the VPC of a private-endpoint cluster lacks DNS support or DNS hostnames.

## Upgrading

Upgrade with `make upgrade` (`scripts/upgrade.py`) from a checkout of the new release, with the same `terraform.tfvars` (or `UPGRADE_FLAGS=--var-file=...`) and the Terraform state of the deployment ([`terraform/backend.tf.example`](../terraform/backend.tf.example) keeps it in S3 with a lock file). First run `make upgrade UPGRADE_FLAGS=--dry-run`: it plans, checks the plan and prints what it would do, and changes nothing. Then `make upgrade`, which:

1. refuses to start while the global kill switch is off, unless this script turned it off in the run it resumes;
2. plans and checks the plan: it stops if the plan deletes or replaces the gateway, a gateway target, the Cognito user pool, domain or a client, the plan table, the audit bucket, the kill switch or the freezes parameter, or replaces an agent's kill switch. It shows the plan and asks before applying (`--yes` skips the question);
3. turns the kill switch off first when the tool set changes (`tools.json` or `classification.json` differ from the deployed `tool_set_hash`), so no agent writes while the gateway and DevOps Agent disagree;
4. applies a known two-step migration first with `-target`, then the plan, and plans and applies once more when Terraform fails with "inconsistent final plan" (it runs `terraform get` before every plan, so a new module is installed). With `--with-k8s` (`make upgrade UPGRADE_FLAGS=--with-k8s`), writes `build/k8s` from the `k8s_manifests` output, runs `kubectl apply --dry-run=server` on all of it, applies it only if that passed, checks that the binding has the `admission_mode`, and lists kube-handrail Roles no longer rendered (it never deletes them). It uses the current kubectl context, or `--kube-context`, resolved once and printed before asking, and names it on every kubectl call; it applies the rendered files by name, nothing else in `build/k8s`. A `--resume` keeps `--with-k8s` and that context. Without `--with-k8s` it warns when the manifests changed; `make upgrade UPGRADE_FLAGS=--k8s-only` then runs only this step against the deployed state;
5. runs `enable-write` when the tool set changed, invokes the drift sentinel and requires `ok`, and turns the kill switch on again only if it turned it off and nothing else wrote it since (a sentinel that finds drift writes `false`, and then it stays off);
6. runs `install-guidance` when `AGENTS.md` or a skill differs from what is installed, prints the asset versions, and warns that running chats may keep the old guidance for about half an hour;
7. runs the smoke test, with `--read-only` in `server` and `both` mode.

`enable-write` and `install-guidance` need the DevOps Agent IDs: `devops_agent_space_id` and `devops_agent_association_id` in tfvars, `AGENT_SPACE_ID` and `ASSOCIATION_ID`, or `--agent-space-id` and `--association-id`; without them both steps are skipped with a warning. The smoke test uses the `smoke_test` agent's client and reads its secret from Cognito (or `CLIENT_ID` and `CLIENT_SECRET`). Progress is kept in `.upgrade-state.json`; after a failure, fix the cause and run `make upgrade UPGRADE_FLAGS=--resume`. Until the apply is done, a resume plans again and turns the kill switch off again if the new plan changes the tool set, also after the switch was turned on by hand. In `client` mode the smoke test scales the sandbox Deployment, so a second upgrade within `flap_window_seconds` fails it with `flapping`: wait, or pass `--skip-smoke`. Plans open across an upgrade may have to be planned again.

Each release's changes, and anything to do by hand, are in [CHANGELOG.md](../CHANGELOG.md); the release rules are in [Releasing](releasing.md). The notes of the releases before `make upgrade` are in [Upgrade notes before Milestone 4](history/upgrading.md). Tested on a test cluster, upgrading from Milestone 3 in 110 seconds.

- **Interceptor and verifier always on** (the release after Milestone 3). `caller_interceptor_enabled` and `verify_enabled` are deprecated and ignored; Terraform warns (check `deprecated_switches`) while either is set, so remove them from your tfvars. On a deployment that had both enabled (the default), `terraform plan` shows 12 moves (`terraform_data.caller_key`, the interceptor's role, log group, role policy and function, and the verifier's role, log group, role policy, function, schedule rule, target and permission, each from `[0]`), rebuilds every package, updates every Lambda in place (the tool Lambdas also drop `CALLER_INTERCEPTOR` and `VERIFY_ENABLED` from their environment) and adds outputs. Any create or destroy of the interceptor, the verifier or their roles is a bug: stop and report it. A deployment with `caller_interceptor_enabled = false` or `verify_enabled = false` gets the missing resources; `make upgrade` creates the caller key, the interceptor and the verifier role first with `-target` (by hand: `terraform apply -target=terraform_data.caller_key -target=aws_iam_role.interceptor -target=aws_lambda_function.interceptor -target=aws_iam_role.verifier`, then `terraform apply`), because the tool Lambdas, the approver and the sentinel read them. The tool schemas do not change.
- **Namespaces and the admission check** (the release after Milestone 4). Implemented, not yet tested on a cluster. Run `make upgrade UPGRADE_FLAGS=--with-k8s` with a kubeconfig for the cluster (cluster-admin), or apply `build/k8s` right after `make upgrade`: until it is applied, every plan is refused with `conflict` (`admission_policy_match`), because the read Lambda now checks the new policy format and needs the new ClusterRole `kube-handrail-policy-reader`. With `namespaces` empty, `terraform plan` rebuilds every package, updates every Lambda in place (the read Lambda gains `ADMISSION_CHECK=true`; `ALLOWED_NAMESPACES`, `MIN_REPLICAS` and `MAX_REPLICAS` keep their values), creates `terraform_data.k8s_manifests`, changes outputs (`guard_settings` gains `namespaces`, `admission_mode`, `admission_check`; new `namespaces`, `k8s_manifests`, `namespace_label_command`) and warns about the deprecated variables if they are set. Any create, destroy or replace of anything else is a bug: stop and report it. The tool schemas do not change. The rendered policy for the deprecated variables equals the earlier one in effect (one namespace, the same bounds, every action) and its binding is in `Deny`; applying it replaces the earlier policy's variables and labels. Then run the label command (`terraform output -raw namespace_label_command`). Between the Terraform apply and the apply of `build/k8s` the new read Lambda refuses every plan (fail closed: `unreadable` while the ClusterRole is missing, `policy_modified` or `bounds_mismatch` against the earlier policy). To skip that window, render and apply the manifests before `make upgrade`; the earlier Lambdas ignore them and the rendered policy enforces the same bounds: `tests/k8s_render/render.sh terraform/terraform.tfvars /tmp/kh-k8s` (needs no credentials; the output directory also holds non-YAML files, so name the YAML files), then `kubectl apply --dry-run=server` and `kubectl apply` with `-f /tmp/kh-k8s/rbac.yaml -f /tmp/kh-k8s/policy-reader.yaml -f /tmp/kh-k8s/admission-policy.yaml`. Planning by hand needs `terraform -chdir=terraform get` (or `init`) once, for the new module.
