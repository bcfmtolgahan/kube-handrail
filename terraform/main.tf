data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  runtime      = "python3.12"
  architecture = "x86_64"
  handler      = "handler.lambda_handler"

  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.region

  repo_root  = abspath("${path.module}/..")
  common_dir = "${local.repo_root}/mcp/common/handrail_common"

  cluster_arn = "arn:${local.partition}:eks:${local.region}:${local.account_id}:cluster/${var.cluster_name}"

  # Must match OBJECT_HISTORY_INDEX in mcp/common/handrail_common/plans.py.
  history_index = "object-history"

  # Gateway target name => Lambda settings. Tool schemas come from the contract's tools.json files.
  functions = {
    k8s_read = {
      target      = "k8sr"
      tools       = jsondecode(file("${path.module}/../mcp/k8s_read/tools.json"))
      dynamo      = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"]
      kill_switch = false
      groups      = ["kube-handrail-reader"]
      # request_approval: moves plans to awaiting_approval and notifies approvers.
      approval_requests = true
    }
    k8s_write = {
      target      = "k8sw"
      tools       = jsondecode(file("${path.module}/../mcp/k8s_write/tools.json"))
      dynamo      = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
      kill_switch = true
      groups      = ["kube-handrail-writer"]

      approval_requests = false
    }
  }

  common_env = {
    CLUSTER_NAME       = var.cluster_name
    PLANS_TABLE        = aws_dynamodb_table.plans.name
    ALLOWED_NAMESPACES = var.sandbox_namespace
    MIN_REPLICAS       = tostring(var.min_replicas)
    MAX_REPLICAS       = tostring(var.max_replicas)
    PLAN_TTL_SECONDS   = tostring(var.plan_ttl_seconds)

    FLAP_WINDOW_SECONDS        = tostring(var.flap_window_seconds)
    OBJECT_HOURLY_APPLY_BUDGET = tostring(var.object_hourly_apply_budget)
    PLAN_RATE_LIMIT            = tostring(var.plan_rate_limit)
    PLAN_RATE_WINDOW_SECONDS   = tostring(var.plan_rate_window_seconds)
    APPLY_COOLDOWN_SECONDS     = tostring(var.apply_cooldown_seconds)
    GLOBAL_HOURLY_APPLY_BUDGET = tostring(var.global_hourly_apply_budget)

    MAX_REPLICA_STEP_PERCENT = tostring(var.max_replica_step_percent)

    APPROVAL_MODE                     = var.approval_mode
    APPROVAL_WAIT_TTL_SECONDS         = tostring(var.approval_wait_ttl_seconds)
    APPLY_AFTER_APPROVAL_TTL_SECONDS  = tostring(var.apply_after_approval_ttl_seconds)
    APPROVAL_REQUEST_COOLDOWN_SECONDS = tostring(var.approval_request_cooldown_seconds)
    APPROVAL_REQUEST_LIMIT            = tostring(var.approval_request_limit)

    AGENTS                    = local.agents_env
    AGENT_KILL_SWITCH_PREFIX  = local.agent_kill_switch_prefix
    AGENT_HOURLY_APPLY_BUDGET = tostring(var.agent_hourly_apply_budget)

    METRIC_NAMESPACE = local.tools_metric_namespace
  }

  # Every packaged Lambda => its requirements file. The sentinel and the approver are packaged
  # but are not gateway targets.
  packages = merge(
    { for fn in keys(local.functions) : fn => "requirements.txt" },
    var.sentinel_enabled ? { sentinel = "mcp/sentinel/requirements.txt" } : {},
    { approver = "mcp/approver/requirements.txt" },
    var.caller_interceptor_enabled ? { interceptor = "mcp/interceptor/requirements.txt" } : {},
  )

  # Tolerates a function directory that has no Python sources yet.
  source_hashes = {
    for fn, requirements in local.packages : fn => sha256(join("", concat(
      [filesha256("${local.repo_root}/${requirements}")],
      [for f in sort(fileset(local.common_dir, "**/*.py")) : filesha256("${local.common_dir}/${f}")],
      [for f in sort(fileset("${local.repo_root}/mcp/${fn}", "**/*.py")) : filesha256("${local.repo_root}/mcp/${fn}/${f}")],
    )))
  }

  scopes = ["read", "remediate"]

  # Agent name => OAuth client_id: the clients created below and the external ones named in
  # var.agents. The gateway allows exactly these, and the tool Lambdas know them as AGENTS.
  agent_client_ids = merge(
    { for name, client in aws_cognito_user_pool_client.this : name => client.id },
    { for name, agent in var.agents : name => agent.client_id if agent.client_id != null },
  )
  # Sorted by agent name; with the default agents this is the list earlier versions allowed.
  allowed_clients = [for name in sort(keys(local.agent_client_ids)) : local.agent_client_ids[name]]
  # client_id => {name, hourly_apply_budget}, as mcp/CONTRACT.md (Agent identity) defines AGENTS.
  agents_env = jsonencode({
    for name, client_id in local.agent_client_ids : client_id => {
      name                = name
      hourly_apply_budget = coalesce(var.agents[name].hourly_apply_budget, var.agent_hourly_apply_budget)
    }
  })
  # Per-agent kill switches are <prefix><name>/enabled.
  agent_kill_switch_prefix = "/${var.name_prefix}/agents/"

  cognito_domain        = "${var.name_prefix}-${local.account_id}"
  cognito_discovery_url = "https://cognito-idp.${local.region}.amazonaws.com/${aws_cognito_user_pool.this.id}/.well-known/openid-configuration"
}

# --- Build and package ---------------------------------------------------------

resource "terraform_data" "build" {
  for_each = local.packages

  triggers_replace = {
    sources = local.source_hashes[each.key]
    runtime = local.runtime
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    working_dir = local.repo_root
    environment = {
      OUT          = "build/${each.key}"
      FN           = each.key
      PYTHON       = var.python
      REQUIREMENTS = each.value
    }
    command = <<-EOT
      set -euo pipefail
      rm -rf "$OUT"
      mkdir -p "$OUT"
      "$PYTHON" -m pip install --quiet --no-compile \
        --target "$OUT" \
        --platform manylinux2014_x86_64 \
        --implementation cp \
        --only-binary=:all: \
        --python-version 3.12 \
        -r "$REQUIREMENTS"
      cp -R mcp/common/handrail_common "$OUT/"
      find "mcp/$FN" -maxdepth 1 -name '*.py' -exec cp {} "$OUT/" \;
      find "$OUT" -name '__pycache__' -type d -prune -exec rm -rf {} +
    EOT
  }
}

data "archive_file" "lambda" {
  for_each = local.packages

  type        = "zip"
  source_dir  = "${local.repo_root}/build/${each.key}"
  output_path = "${local.repo_root}/build/${each.key}.zip"

  # Defers the read to apply time whenever the build re-runs.
  depends_on = [terraform_data.build]
}

# --- Lambda functions and their roles ---------------------------------------

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each = local.functions

  name               = "${var.name_prefix}-${replace(each.key, "_", "-")}"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each = local.functions

  name              = "/aws/lambda/${var.name_prefix}-${replace(each.key, "_", "-")}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "lambda" {
  for_each = local.functions

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda[each.key].arn}:*"]
  }

  statement {
    sid       = "DescribeCluster"
    actions   = ["eks:DescribeCluster"]
    resources = [local.cluster_arn]
  }

  statement {
    sid       = "Plans"
    actions   = each.value.dynamo
    resources = [aws_dynamodb_table.plans.arn]
  }

  statement {
    sid       = "PlanHistory"
    actions   = ["dynamodb:Query"]
    resources = ["${aws_dynamodb_table.plans.arn}/index/${local.history_index}"]
  }

  dynamic "statement" {
    for_each = each.value.kill_switch ? [1] : []
    content {
      sid       = "KillSwitch"
      actions   = ["ssm:GetParameter"]
      resources = [aws_ssm_parameter.kill_switch.arn]
    }
  }

  # Both Lambdas read the per-agent kill switches: plan and request_approval as well as apply.
  statement {
    sid       = "AgentKillSwitches"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:${local.partition}:ssm:${local.region}:${local.account_id}:parameter${local.agent_kill_switch_prefix}*"]
  }

  dynamic "statement" {
    for_each = each.value.approval_requests ? [1] : []
    content {
      sid       = "ApprovalRequests"
      actions   = ["sns:Publish"]
      resources = [aws_sns_topic.approvals.arn]
    }
  }

  # The read role cannot write approval attributes, even if its code is compromised. Not on
  # the write role: its consume condition reads these attributes.
  dynamic "statement" {
    for_each = each.value.approval_requests ? [1] : []
    content {
      sid       = "NeverApprove"
      effect    = "Deny"
      actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
      resources = [aws_dynamodb_table.plans.arn]
      condition {
        test     = "ForAnyValue:StringEquals"
        variable = "dynamodb:Attributes"
        values   = local.approval_attributes
      }
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.functions

  name   = "runtime"
  role   = aws_iam_role.lambda[each.key].id
  policy = data.aws_iam_policy_document.lambda[each.key].json
}

# The network interface permissions are a separate policy, which exists only while the tool
# Lambdas are in a VPC. It is deleted after the security group: Lambda deletes its network
# interfaces with these permissions, and the provider deletes leftover ones before the group.
data "aws_iam_policy_document" "lambda_vpc" {
  for_each = local.lambda_in_vpc ? local.functions : {}

  # What AWSLambdaVPCAccessExecutionRole grants, without its logs:* on every log group.
  statement {
    sid = "VpcNetworkInterfaces"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeSubnets",
      "ec2:DeleteNetworkInterface",
      "ec2:AssignPrivateIpAddresses",
      "ec2:UnassignPrivateIpAddresses",
    ]
    resources = ["*"]
  }

  # Lambda grants the role's network interface actions to the function's code as well. Only
  # the Lambda service needs them; requests from the code carry lambda:SourceFunctionArn.
  # The ARN is built from the name: referencing aws_lambda_function.this would be a cycle.
  statement {
    sid    = "VpcNetworkInterfacesNotFromCode"
    effect = "Deny"
    actions = [
      "ec2:CreateNetworkInterface",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeSubnets",
      "ec2:DetachNetworkInterface",
      "ec2:DeleteNetworkInterface",
      "ec2:AssignPrivateIpAddresses",
      "ec2:UnassignPrivateIpAddresses",
    ]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "lambda:SourceFunctionArn"
      values   = ["arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${var.name_prefix}-${replace(each.key, "_", "-")}"]
    }
  }
}

resource "aws_iam_role_policy" "lambda_vpc" {
  for_each = data.aws_iam_policy_document.lambda_vpc

  name   = "vpc-network-interfaces"
  role   = aws_iam_role.lambda[each.key].id
  policy = each.value.json
}

resource "aws_lambda_function" "this" {
  for_each = local.functions

  function_name    = "${var.name_prefix}-${replace(each.key, "_", "-")}"
  role             = aws_iam_role.lambda[each.key].arn
  runtime          = local.runtime
  architectures    = [local.architecture]
  handler          = local.handler
  timeout          = 30
  memory_size      = 256
  filename         = data.archive_file.lambda[each.key].output_path
  source_code_hash = data.archive_file.lambda[each.key].output_base64sha256

  environment {
    variables = merge(
      local.common_env,
      each.value.kill_switch ? { REMEDIATION_ENABLED_PARAM = aws_ssm_parameter.kill_switch.name } : {},
      { CALLER_INTERCEPTOR = tostring(var.caller_interceptor_enabled) },
      local.caller_key_env,
      each.value.approval_requests ? {
        APPROVAL_TOPIC_ARN = aws_sns_topic.approvals.arn
        APPROVAL_URL       = aws_lambda_function_url.approver.function_url
      } : {},
    )
  }

  dynamic "vpc_config" {
    for_each = local.lambda_in_vpc ? [1] : []
    content {
      subnet_ids         = var.lambda_subnet_ids
      security_group_ids = local.lambda_security_group_ids
    }
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda, aws_iam_role_policy.lambda_vpc]

  lifecycle {
    precondition {
      condition     = var.min_replicas >= 0 && var.min_replicas <= var.max_replicas
      error_message = "Require 0 <= min_replicas <= max_replicas."
    }
    precondition {
      condition     = local.cluster_vpc_config.endpoint_public_access || local.lambda_in_vpc
      error_message = "The cluster has no public endpoint. Set lambda_subnet_ids to subnets that reach its private endpoint (docs/deploy.md, Network)."
    }
    precondition {
      condition     = local.lambda_in_vpc || (length(var.lambda_security_group_ids) == 0 && !var.allow_lambda_to_cluster_endpoint && !var.create_vpc_endpoints)
      error_message = "lambda_security_group_ids, allow_lambda_to_cluster_endpoint and create_vpc_endpoints need lambda_subnet_ids."
    }
    # Without private access, requests from the VPC go to the public endpoint, which the
    # cluster security group does not filter and no VPC endpoint carries.
    precondition {
      condition     = !var.allow_lambda_to_cluster_endpoint || local.cluster_vpc_config.endpoint_private_access
      error_message = "allow_lambda_to_cluster_endpoint needs private endpoint access on the cluster: the cluster security group does not filter the public endpoint."
    }
    precondition {
      condition     = !var.create_vpc_endpoints || local.cluster_vpc_config.endpoint_private_access
      error_message = "create_vpc_endpoints needs private endpoint access on the cluster: no VPC endpoint carries Kubernetes API traffic, so the Lambdas could not reach the public endpoint without a NAT gateway."
    }
  }
}

# --- Plans table and kill switch --------------------------------------------

resource "aws_dynamodb_table" "plans" {
  name         = "${var.name_prefix}-plans"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "plan_id"

  attribute {
    name = "plan_id"
    type = "S"
  }

  attribute {
    name = "object_key"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "N"
  }

  # Plan history per object (namespace/name/uid), read by the oscillation breaker.
  global_secondary_index {
    name            = local.history_index
    projection_type = "ALL"

    key_schema {
      attribute_name = "object_key"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "created_at"
      key_type       = "RANGE"
    }
  }

  # Records outlive their plan (expires_at) so they can serve as history.
  ttl {
    attribute_name = "retain_until"
    enabled        = true
  }
}

resource "aws_ssm_parameter" "kill_switch" {
  name        = "/${var.name_prefix}/remediation-enabled"
  description = "Only the exact value \"true\" allows apply_scale_deployment to run."
  type        = "String"
  value       = "true"

  # Operators flip this out of band; a later apply must not reset it.
  lifecycle {
    ignore_changes = [value]
  }
}

# One kill switch per agent. Only operators write them; the drift sentinel never does.
resource "aws_ssm_parameter" "agent_enabled" {
  for_each = var.agents

  name        = "${local.agent_kill_switch_prefix}${each.key}/enabled"
  description = "Only the exact value \"true\" lets agent ${each.key} plan, request approval and apply."
  type        = "String"
  value       = "true"

  lifecycle {
    ignore_changes = [value]
  }
}

# --- EKS access (RBAC bindings live in the cluster) -------------------------

resource "aws_eks_access_entry" "lambda" {
  for_each = local.functions

  cluster_name      = var.cluster_name
  principal_arn     = aws_iam_role.lambda[each.key].arn
  type              = "STANDARD"
  kubernetes_groups = each.value.groups

  lifecycle {
    precondition {
      condition     = contains(["API", "API_AND_CONFIG_MAP"], coalesce(local.authentication_mode, "unknown"))
      error_message = "Access entries need the cluster's authentication mode to be API or API_AND_CONFIG_MAP; it is ${coalesce(local.authentication_mode, "unknown")}. Change it with aws eks update-cluster-config --access-config authenticationMode=API_AND_CONFIG_MAP."
    }
  }
}

# --- Inbound auth: Cognito client credentials -------------------------------

resource "aws_cognito_user_pool" "this" {
  name = var.name_prefix
}

resource "aws_cognito_user_pool_domain" "this" {
  domain       = local.cognito_domain
  user_pool_id = aws_cognito_user_pool.this.id
}

resource "aws_cognito_resource_server" "this" {
  identifier   = var.name_prefix
  name         = var.name_prefix
  user_pool_id = aws_cognito_user_pool.this.id

  dynamic "scope" {
    for_each = local.scopes
    content {
      scope_name        = scope.value
      scope_description = "kube-handrail ${scope.value} tools"
    }
  }
}

# One client per agent without an external client_id. The keys are agent names, as the
# fixed set (devops_agent, smoke_test) was before, so those clients keep their addresses.
resource "aws_cognito_user_pool_client" "this" {
  for_each = { for name, agent in var.agents : name => agent if agent.client_id == null }

  name                                 = "${var.name_prefix}-${replace(each.key, "_", "-")}"
  user_pool_id                         = aws_cognito_user_pool.this.id
  generate_secret                      = true
  allowed_oauth_flows                  = ["client_credentials"]
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_scopes                 = [for s in local.scopes : "${aws_cognito_resource_server.this.identifier}/${s}"]
  supported_identity_providers         = ["COGNITO"]
}

# --- AgentCore gateway ------------------------------------------------------

data "aws_iam_policy_document" "gateway_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name               = "${var.name_prefix}-gateway"
  assume_role_policy = data.aws_iam_policy_document.gateway_assume.json
}

data "aws_iam_policy_document" "gateway" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = concat([for fn in aws_lambda_function.this : fn.arn], aws_lambda_function.interceptor[*].arn)
  }
}

resource "aws_iam_role_policy" "gateway" {
  name   = "invoke-tools"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway.json
}

resource "aws_bedrockagentcore_gateway" "this" {
  name            = var.name_prefix
  protocol_type   = "MCP"
  role_arn        = aws_iam_role.gateway.arn
  authorizer_type = "CUSTOM_JWT"

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url   = local.cognito_discovery_url
      allowed_clients = local.allowed_clients
    }
  }

  dynamic "interceptor_configuration" {
    for_each = aws_lambda_function.interceptor
    content {
      interception_points = ["REQUEST"]
      interceptor {
        lambda {
          arn = interceptor_configuration.value.arn
        }
      }
      input_configuration {
        pass_request_headers = true
      }
    }
  }

  # The gateway role must be allowed to invoke the interceptor before the gateway uses it.
  depends_on = [aws_iam_role_policy.gateway]
}

resource "aws_bedrockagentcore_gateway_target" "this" {
  for_each = local.functions

  name               = each.value.target
  gateway_identifier = aws_bedrockagentcore_gateway.this.gateway_id

  credential_provider_configuration {
    gateway_iam_role {}
  }

  target_configuration {
    mcp {
      lambda {
        lambda_arn = aws_lambda_function.this[each.key].arn

        tool_schema {
          dynamic "inline_payload" {
            for_each = each.value.tools
            content {
              name        = inline_payload.value.name
              description = inline_payload.value.description

              input_schema {
                type = inline_payload.value.inputSchema.type

                dynamic "property" {
                  for_each = inline_payload.value.inputSchema.properties
                  content {
                    name        = property.key
                    type        = property.value.type
                    description = lookup(property.value, "description", null)
                    required    = contains(lookup(inline_payload.value.inputSchema, "required", []), property.key)
                  }
                }
              }
            }
          }
        }
      }
    }
  }

  depends_on = [aws_iam_role_policy.gateway]

  lifecycle {
    precondition {
      condition = alltrue([
        for t in each.value.tools :
        length("${each.value.target}___${t.name}") <= 64 && can(regex("^[a-z0-9_]+$", "${each.value.target}___${t.name}"))
      ])
      error_message = "Every <target>___<tool> name must be at most 64 characters and match ^[a-z0-9_]+$."
    }
  }
}
