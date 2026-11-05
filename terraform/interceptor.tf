# Gateway request interceptor (mcp/CONTRACT.md, Agent identity). On by default: it copies the
# verified token's client_id into a reserved tool argument, signed with a key only it and the
# tool Lambdas hold, by which the tool Lambdas identify the agent. Without it (legacy mode)
# plans name no agent and per-agent limits do not apply.

locals {
  interceptor_count = var.caller_interceptor_enabled ? 1 : 0
  interceptor_name  = "${var.name_prefix}-interceptor"
  # CALLER_KEY for the interceptor and both tool Lambdas; empty in legacy mode.
  caller_key_env = var.caller_interceptor_enabled ? { CALLER_KEY = sensitive(terraform_data.caller_key[0].output) } : {}
}

# The caller signing key: random once, then kept (ignore_changes), so it changes only when this
# resource is replaced. A client never sees it, so a client's own caller argument fails the
# signature check even when a gateway passes it through without the interceptor.
resource "terraform_data" "caller_key" {
  count = local.interceptor_count

  input = uuid()

  lifecycle {
    ignore_changes = [input]
  }
}

resource "aws_iam_role" "interceptor" {
  count = local.interceptor_count

  name               = local.interceptor_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "interceptor" {
  count = local.interceptor_count

  name              = "/aws/lambda/${local.interceptor_name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "interceptor" {
  count = local.interceptor_count

  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.interceptor[0].arn}:*"]
  }
}

resource "aws_iam_role_policy" "interceptor" {
  count = local.interceptor_count

  name   = "runtime"
  role   = aws_iam_role.interceptor[0].id
  policy = data.aws_iam_policy_document.interceptor[0].json
}

check "agent_identity" {
  assert {
    condition     = var.caller_interceptor_enabled
    error_message = "caller_interceptor_enabled is false (legacy mode): plans name no agent, and per-agent attribution, budgets and kill switches do not apply."
  }
}

resource "aws_lambda_function" "interceptor" {
  count = local.interceptor_count

  function_name    = local.interceptor_name
  role             = aws_iam_role.interceptor[0].arn
  runtime          = local.runtime
  architectures    = [local.architecture]
  handler          = local.handler
  timeout          = 5
  memory_size      = 128
  filename         = data.archive_file.lambda["interceptor"].output_path
  source_code_hash = data.archive_file.lambda["interceptor"].output_base64sha256

  environment {
    variables = local.caller_key_env
  }

  depends_on = [aws_cloudwatch_log_group.interceptor, aws_iam_role_policy.interceptor]
}
