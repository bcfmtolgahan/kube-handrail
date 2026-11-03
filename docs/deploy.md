# Deploying to a new account

What a deployment needs from the account, the cluster and the network, and the order of the steps. The commands for each step, and what the tests check, are in [Milestone 0](milestone-0.md). The runtime calls and IAM permissions are in the [contract](../mcp/CONTRACT.md#network).

Status: implemented and checked with `terraform validate`. A fresh deployment, and one against a private-endpoint cluster, have not been run yet.

## Prerequisites

- **Cluster.** An existing EKS cluster whose authentication mode is `API` or `API_AND_CONFIG_MAP`, because the Lambdas get cluster access through access entries. Terraform reads the cluster and stops before creating anything if the mode is `CONFIG_MAP`. Switching the mode to `API_AND_CONFIG_MAP` keeps the `aws-auth` ConfigMap working:

  ```sh
  aws eks update-cluster-config --name <cluster> --access-config authenticationMode=API_AND_CONFIG_MAP
  ```

- **Region.** Amazon Bedrock AgentCore Gateway and AWS DevOps Agent must be available in the Region of the cluster.
- **Operator credentials.** Rights to read the cluster (`eks:DescribeCluster`) and to create IAM roles and policies, Lambda functions and Function URLs, a DynamoDB table, an SSM parameter, SNS topics, a Cognito user pool, an AgentCore gateway, EventBridge rules, CloudWatch log groups and alarms, and EKS access entries; with the network options below, also security groups, security group rules and VPC endpoints, and the EC2 read actions `ec2:DescribeSubnets`, `ec2:DescribeVpcs`, `ec2:DescribeVpcAttribute`, `ec2:DescribeRouteTables`, `ec2:DescribeVpcEndpointServices`, `ec2:DescribeSecurityGroups` and `ec2:GetSecurityGroupsForVpc` (Lambda checks the last two on the caller that attaches a VPC). Destroying VPC Lambdas also needs `ec2:DescribeNetworkInterfaces`, `ec2:DetachNetworkInterface` and `ec2:DeleteNetworkInterface` (below). Applying the sandbox RBAC needs a kubeconfig with cluster-admin rights that can reach the cluster's API server; for a private endpoint that means from inside the VPC or over a VPN.
- **Build machine.** Terraform 1.5 or later (the AWS provider is pinned to `~> 6.70` in `terraform/versions.tf`, and `terraform/.terraform.lock.hcl` pins the exact versions), `/bin/bash`, and a Python with `pip` given by the `python` variable (default `python3`). The build runs `pip install --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12`, so the interpreter's own version does not matter, but the machine needs access to PyPI during `terraform apply`.

## Names

Every AWS resource name starts with `name_prefix` (default `kube-handrail`): IAM roles and the approver policy (`<prefix>-k8s-read`, `-k8s-write`, `-gateway`, `-approver`, `-approver-invoke`, `-sentinel`, `-interceptor`), Lambda functions and their log groups (`/aws/lambda/<prefix>-...`), the plan table (`<prefix>-plans`), the kill switch (`/<prefix>/remediation-enabled`), SNS topics (`<prefix>-approvals`, `<prefix>-alerts`), the Cognito user pool and resource server, the gateway, the sentinel rule and alarms, and the security groups (`<prefix>-lambda`, `<prefix>-vpc-endpoints`).

- IAM names are global to the account. Two deployments in one account need different prefixes, also when they are in different Regions.
- The Cognito domain is `<prefix>-<account ID>`, so it does not collide with other accounts. Cognito does not allow `aws`, `amazon` or `cognito` in a domain, so the prefix may not contain them either; Terraform rejects such a prefix.
- A log group left over from an earlier deployment with the same prefix makes the apply fail with "already exists". Delete it, or import it into the state.

Not derived from `name_prefix`:

- The Kubernetes groups `kube-handrail-reader` and `kube-handrail-writer`, which the access entries map the Lambda roles to. Deployments on the same cluster share them, and with them the RoleBindings.
- The namespace in `k8s/sandbox/*.yaml` and in `tests/e2e/e2e_gateway.py`, `kube-handrail-sandbox`, which is also the default of `sandbox_namespace`. `scripts/smoke_test.py` takes `NAMESPACE`.

## Namespace and RBAC

The Lambdas can do nothing in the cluster until the Roles and RoleBindings in `k8s/sandbox/rbac.yaml` exist in the allowed namespace. For the default namespace, `kubectl apply -f k8s/sandbox/` creates the namespace, the RBAC and the `web` test Deployment ([Milestone 0, step 1](milestone-0.md#1-sandbox-namespace-rbac-and-target-workload)). For another `sandbox_namespace`, create the namespace and apply the same RBAC there; the group names must stay as they are:

```sh
sed 's/kube-handrail-sandbox/<namespace>/' k8s/sandbox/rbac.yaml | kubectl apply -f -
```

The end-to-end guard tests only run against `kube-handrail-sandbox`.

## Network

The tool Lambdas (`k8s_read` and `k8s_write`) call the cluster's API server, `eks:DescribeCluster`, DynamoDB, SSM (write) and SNS (read), all over HTTPS. They never call STS or CloudWatch Logs. The approver, sentinel and interceptor Lambdas do not talk to the cluster and always run outside any VPC. Details are in the [contract](../mcp/CONTRACT.md#network).

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
4. **AWS APIs.** The subnets need a route to DynamoDB, EKS, SSM and SNS in the Region: either a NAT gateway, or VPC endpoints. `create_vpc_endpoints = true` creates interface endpoints with private DNS for `eks`, `ssm` and `sns` (one subnet per Availability Zone from `lambda_subnet_ids`, with a security group `<prefix>-vpc-endpoints` that accepts TCP 443 from the Lambdas) and a DynamoDB gateway endpoint on the route tables of the Lambda subnets (the VPC's main route table for a subnet without an explicit association). It needs private endpoint access on the cluster: no VPC endpoint carries Kubernetes API traffic. Leave it off if the VPC already has endpoints for any of these services: a second interface endpoint with private DNS for the same service, or a second DynamoDB gateway endpoint on the same route table, fails to create. Existing endpoints must then accept TCP 443 from the Lambdas' security groups, and their endpoint policies must allow the calls in the contract. Interface endpoints are billed per hour and Availability Zone.

If any of these is missing, a tool call does not return an error envelope: the Lambda waits on the connection until its 30-second timeout and the call fails. Check the Lambda's log group for a timeout.

On `terraform destroy`, Lambda releases the network interfaces of VPC functions only some time after the functions are deleted, and needs the role's permissions to do so. Terraform therefore removes the role policies only after the `<prefix>-lambda` security group, and before deleting that group the AWS provider deletes the network interfaces Lambda left behind, with the operator's credentials. Deleting the group can wait many minutes. With your own `lambda_security_group_ids`, or when one apply moves the Lambdas out of a VPC, the role permissions can go first; Lambda then cannot delete the interfaces, and they remain until you delete them. If deleting the group fails on a dependency violation, find the network interfaces that still use the group, delete them, and run `destroy` again.

## Order of steps

1. **Sandbox RBAC.** Apply the namespace and RBAC (above). The bindings name the groups, not the roles, so they can exist before the roles do.
2. **Variables.** Copy `terraform/terraform.tfvars.example` to `terraform/terraform.tfvars` and set at least `cluster_name`, `region` and, for a private endpoint, the network variables. For the first tests set `approval_mode = "client"`: the smoke test and the default guard tests apply through the gateway, which `server` mode refuses without an approval.
3. **Infrastructure.** `terraform -chdir=terraform init`, `plan`, `apply` ([Milestone 0, step 2](milestone-0.md#2-infrastructure)). The plan fails early if the authentication mode, the endpoint access or the network variables do not fit together.
4. **Smoke and guard tests** through the gateway ([Milestone 0, step 3](milestone-0.md#3-smoke-test-through-the-gateway)). On a private-endpoint cluster, `kubectl` for the guard tests must reach the API server; the tool calls themselves go through the gateway and need no network access to the cluster from where they are run.
5. **Register with AWS DevOps Agent**: `create-space`, `register-mcp`, `associate-read`, check in chat, then `enable-write` ([Milestone 0, step 4](milestone-0.md#4-register-with-aws-devops-agent)).
6. **Let the sentinel watch the association.** Set `devops_agent_space_id` and `devops_agent_association_id` and apply again.
7. **Server-side approval.** Set `approval_mode = "server"` (or `both`) and `approver_principals`, apply, and attach the `approver_policy_arn` policy to each approver ([Server-side approval](milestone-0.md#server-side-approval)). The tool set does not change, so the sentinel sees no drift.
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
