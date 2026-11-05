# --- Cluster and network (docs/deploy.md, Network) ---------------------------
#
# Only the two tool Lambdas call the EKS API, so only they can run in a VPC. At runtime
# they call eks:DescribeCluster, the cluster's API server, DynamoDB, SSM (write) and SNS
# (read). The EKS token is presigned locally and never sent to STS by the Lambda, and
# Lambda delivers logs outside the function's network, so neither STS nor CloudWatch Logs
# needs to be reachable from the subnets.

data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

locals {
  cluster_vpc_config = one(data.aws_eks_cluster.this.vpc_config)
  # null when the cluster API does not report it; the access entry precondition fails on it.
  authentication_mode = try(one(data.aws_eks_cluster.this.access_config).authentication_mode, null)

  lambda_in_vpc        = length(var.lambda_subnet_ids) > 0
  create_lambda_sg     = local.lambda_in_vpc && length(var.lambda_security_group_ids) == 0
  lambda_vpc_id        = local.lambda_in_vpc ? data.aws_subnet.lambda[var.lambda_subnet_ids[0]].vpc_id : null
  create_vpc_endpoints = local.lambda_in_vpc && var.create_vpc_endpoints

  lambda_security_group_ids = local.create_lambda_sg ? aws_security_group.lambda[*].id : var.lambda_security_group_ids

  # An interface endpoint takes at most one subnet per Availability Zone.
  lambda_subnets_by_az  = { for s in data.aws_subnet.lambda : s.availability_zone => s.id... }
  endpoint_subnet_ids   = [for az in sort(keys(local.lambda_subnets_by_az)) : sort(local.lambda_subnets_by_az[az])[0]]
  interface_endpoints   = toset(["eks", "ssm", "sns"])
  cluster_https_sources = local.lambda_in_vpc && var.allow_lambda_to_cluster_endpoint ? local.lambda_security_group_ids : []

  # A subnet without an explicit association uses the VPC's main route table.
  endpoint_route_table_ids = local.create_vpc_endpoints ? sort(distinct(flatten([
    for id in var.lambda_subnet_ids : coalescelist(data.aws_route_tables.lambda_subnet[id].ids, data.aws_route_tables.main[0].ids)
  ]))) : []
}

data "aws_subnet" "lambda" {
  for_each = toset(var.lambda_subnet_ids)

  id = each.value
}

data "aws_vpc" "lambda" {
  count = local.lambda_in_vpc ? 1 : 0

  id = local.lambda_vpc_id
}

resource "aws_security_group" "lambda" {
  count = local.create_lambda_sg ? 1 : 0

  name        = "${var.name_prefix}-lambda"
  description = "kube-handrail tool Lambdas: outbound HTTPS to the EKS API and AWS APIs."
  vpc_id      = local.lambda_vpc_id

  # Lambda deletes its network interfaces with the role's permissions, and the provider
  # deletes leftover ones before the group. Keep that policy until the group is gone.
  depends_on = [aws_iam_role_policy.lambda_vpc]

  # Terraform otherwise deletes a dependency before it updates the resources that use it.
  # When one apply moves the Lambdas out of a VPC, the group (and after it the policy) must
  # go only after the functions have been updated and released their network interfaces;
  # deleting it first waits on those interfaces until the provider times out.
  lifecycle {
    create_before_destroy = true
  }
}

# Every call the tool Lambdas make is HTTPS. Terraform removes the default allow-all rule.
resource "aws_vpc_security_group_egress_rule" "lambda_https" {
  count = local.create_lambda_sg ? 1 : 0

  security_group_id = aws_security_group.lambda[0].id
  description       = "HTTPS to the EKS API server and AWS APIs"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

# Keyed by index: the id of a security group created in the same apply is unknown at plan time.
resource "aws_vpc_security_group_ingress_rule" "cluster_from_lambda" {
  count = length(local.cluster_https_sources)

  security_group_id            = local.cluster_vpc_config.cluster_security_group_id
  description                  = "${var.name_prefix} tool Lambdas to the EKS API server"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = local.cluster_https_sources[count.index]
}

# --- VPC endpoints (create_vpc_endpoints) -------------------------------------

data "aws_vpc_endpoint_service" "interface" {
  for_each = local.create_vpc_endpoints ? local.interface_endpoints : toset([])

  service      = each.key
  service_type = "Interface"
}

data "aws_vpc_endpoint_service" "dynamodb" {
  count = local.create_vpc_endpoints ? 1 : 0

  service      = "dynamodb"
  service_type = "Gateway"
}

data "aws_route_tables" "lambda_subnet" {
  for_each = local.create_vpc_endpoints ? toset(var.lambda_subnet_ids) : toset([])

  filter {
    name   = "association.subnet-id"
    values = [each.value]
  }
}

data "aws_route_tables" "main" {
  count = local.create_vpc_endpoints ? 1 : 0

  vpc_id = local.lambda_vpc_id

  filter {
    name   = "association.main"
    values = ["true"]
  }
}

resource "aws_security_group" "endpoints" {
  count = local.create_vpc_endpoints ? 1 : 0

  name        = "${var.name_prefix}-vpc-endpoints"
  description = "kube-handrail interface endpoints: inbound HTTPS from the VPC."
  vpc_id      = local.lambda_vpc_id
}

# Private DNS sends every client in the VPC to these endpoints, not only the tool Lambdas.
# Accepting only the Lambdas would cut off other workloads that call EKS, SSM or SNS, so
# the endpoints accept HTTPS from the VPC's address ranges; IAM still authorizes each call.
resource "aws_vpc_security_group_ingress_rule" "endpoints_from_vpc" {
  for_each = local.create_vpc_endpoints ? toset([for a in data.aws_vpc.lambda[0].cidr_block_associations : a.cidr_block]) : toset([])

  security_group_id = aws_security_group.endpoints[0].id
  description       = "HTTPS from the VPC"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = each.value
}

resource "aws_vpc_endpoint" "interface" {
  for_each = data.aws_vpc_endpoint_service.interface

  vpc_id              = local.lambda_vpc_id
  service_name        = each.value.service_name
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.endpoint_subnet_ids
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true

  lifecycle {
    precondition {
      condition     = data.aws_vpc.lambda[0].enable_dns_support && data.aws_vpc.lambda[0].enable_dns_hostnames
      error_message = "Private DNS for the interface endpoints needs enableDnsSupport and enableDnsHostnames on the VPC of lambda_subnet_ids."
    }
  }
}

resource "aws_vpc_endpoint" "dynamodb" {
  count = local.create_vpc_endpoints ? 1 : 0

  vpc_id            = local.lambda_vpc_id
  service_name      = data.aws_vpc_endpoint_service.dynamodb[0].service_name
  vpc_endpoint_type = "Gateway"
  route_table_ids   = local.endpoint_route_table_ids
}

# Warnings, not errors: these setups can work with extra configuration outside this module.
check "lambda_network" {
  assert {
    condition     = local.lambda_in_vpc || !local.cluster_vpc_config.endpoint_public_access || contains(local.cluster_vpc_config.public_access_cidrs, "0.0.0.0/0")
    error_message = "The cluster's public endpoint is limited to public_access_cidrs, and Lambdas outside a VPC have no fixed source address. Set lambda_subnet_ids unless those CIDRs cover the Lambda service's addresses."
  }

  assert {
    condition     = local.lambda_in_vpc ? local.lambda_vpc_id == local.cluster_vpc_config.vpc_id : true
    error_message = "lambda_subnet_ids are not in the cluster's VPC. The private endpoint must then be reachable and resolvable from that VPC (peering or Transit Gateway, and DNS), and allow_lambda_to_cluster_endpoint may not apply."
  }

  assert {
    condition     = local.lambda_in_vpc && local.cluster_vpc_config.endpoint_private_access ? data.aws_vpc.lambda[0].enable_dns_support && data.aws_vpc.lambda[0].enable_dns_hostnames : true
    error_message = "The cluster's private endpoint resolves inside the VPC only with enableDnsSupport and enableDnsHostnames on."
  }

  assert {
    condition     = !local.lambda_in_vpc || local.cluster_vpc_config.endpoint_private_access
    error_message = "The cluster has no private endpoint access, so the Lambdas in lambda_subnet_ids reach its public endpoint through a NAT gateway. The NAT gateway's address must be in public_access_cidrs."
  }
}
