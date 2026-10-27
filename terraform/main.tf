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

  # Gateway target name => Lambda settings. Tool schemas come from the contract's tools.json files.
  functions = {
    k8s_read = {
      target      = "k8sr"
      tools       = jsondecode(file("${path.module}/../mcp/k8s_read/tools.json"))
      dynamo      = ["dynamodb:PutItem"]
      kill_switch = false
      groups      = ["kube-handrail-reader"]
    }
    k8s_write = {
      target      = "k8sw"
      tools       = jsondecode(file("${path.module}/../mcp/k8s_write/tools.json"))
      dynamo      = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
      kill_switch = true
      groups      = ["kube-handrail-writer"]
    }
  }

  common_env = {
    CLUSTER_NAME       = var.cluster_name
    PLANS_TABLE        = aws_dynamodb_table.plans.name
    ALLOWED_NAMESPACES = var.sandbox_namespace
    MIN_REPLICAS       = tostring(var.min_replicas)
    MAX_REPLICAS       = tostring(var.max_replicas)
    PLAN_TTL_SECONDS   = tostring(var.plan_ttl_seconds)
  }

  # Tolerates a function directory that has no Python sources yet.
  source_hashes = {
    for fn in keys(local.functions) : fn => sha256(join("", concat(
      [filesha256("${local.repo_root}/requirements.txt")],
      [for f in sort(fileset(local.common_dir, "**/*.py")) : filesha256("${local.common_dir}/${f}")],
      [for f in sort(fileset("${local.repo_root}/mcp/${fn}", "**/*.py")) : filesha256("${local.repo_root}/mcp/${fn}/${f}")],
    )))
  }

  scopes = ["read", "remediate"]

  cognito_domain        = "${var.name_prefix}-${local.account_id}"
  cognito_discovery_url = "https://cognito-idp.${local.region}.amazonaws.com/${aws_cognito_user_pool.this.id}/.well-known/openid-configuration"
}

# --- Build and package ---------------------------------------------------------

resource "terraform_data" "build" {
  for_each = local.functions

  triggers_replace = {
    sources = local.source_hashes[each.key]
    runtime = local.runtime
  }

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    working_dir = local.repo_root
    environment = {
      OUT    = "build/${each.key}"
      FN     = each.key
      PYTHON = var.python
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
        -r requirements.txt
      cp -R mcp/common/handrail_common "$OUT/"
      find "mcp/$FN" -maxdepth 1 -name '*.py' -exec cp {} "$OUT/" \;
      find "$OUT" -name '__pycache__' -type d -prune -exec rm -rf {} +
    EOT
  }
}

data "archive_file" "lambda" {
  for_each = local.functions

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

  dynamic "statement" {
    for_each = each.value.kill_switch ? [1] : []
    content {
      sid       = "KillSwitch"
      actions   = ["ssm:GetParameter"]
      resources = [aws_ssm_parameter.kill_switch.arn]
    }
  }
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.functions

  name   = "runtime"
  role   = aws_iam_role.lambda[each.key].id
  policy = data.aws_iam_policy_document.lambda[each.key].json
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
    )
  }

  depends_on = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.lambda]

  lifecycle {
    precondition {
      condition     = var.min_replicas >= 0 && var.min_replicas <= var.max_replicas
      error_message = "Require 0 <= min_replicas <= max_replicas."
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

  ttl {
    attribute_name = "expires_at"
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

# --- EKS access (RBAC bindings live in the cluster) -------------------------

resource "aws_eks_access_entry" "lambda" {
  for_each = local.functions

  cluster_name      = var.cluster_name
  principal_arn     = aws_iam_role.lambda[each.key].arn
  type              = "STANDARD"
  kubernetes_groups = each.value.groups
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

resource "aws_cognito_user_pool_client" "this" {
  for_each = toset(["devops_agent", "smoke_test"])

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
    resources = [for fn in aws_lambda_function.this : fn.arn]
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
      allowed_clients = [for c in aws_cognito_user_pool_client.this : c.id]
    }
  }
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
