# Gateway request interceptor (mcp/CONTRACT.md, Agent identity). Always deployed: it copies
# the verified token's client_id into a reserved tool argument, signed with a key only it and
# the tool Lambdas hold, by which the tool Lambdas identify the agent.

locals {
  interceptor_name = "${var.name_prefix}-interceptor"
  # CALLER_KEY for the interceptor and both tool Lambdas.
  caller_key_env = { CALLER_KEY = sensitive(terraform_data.caller_key.output) }
}

# The caller signing key: random once, then kept (ignore_changes), so it changes only when this
# resource is replaced. A client never sees it, so a client's own caller argument fails the
# signature check even when a gateway passes it through without the interceptor.
resource "terraform_data" "caller_key" {
  input = uuid()

  lifecycle {
    ignore_changes = [input]
  }
}

resource "aws_iam_role" "interceptor" {
  name               = local.interceptor_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "interceptor" {
  name              = "/aws/lambda/${local.interceptor_name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "interceptor" {
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.interceptor.arn}:*"]
  }
}

resource "aws_iam_role_policy" "interceptor" {
  name   = "runtime"
  role   = aws_iam_role.interceptor.id
  policy = data.aws_iam_policy_document.interceptor.json
}

resource "aws_lambda_function" "interceptor" {
  function_name    = local.interceptor_name
  role             = aws_iam_role.interceptor.arn
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

# The resources had count = 1 while caller_interceptor_enabled was optional; existing
# deployments keep them under their new addresses.
moved {
  from = terraform_data.caller_key[0]
  to   = terraform_data.caller_key
}

moved {
  from = aws_iam_role.interceptor[0]
  to   = aws_iam_role.interceptor
}

moved {
  from = aws_cloudwatch_log_group.interceptor[0]
  to   = aws_cloudwatch_log_group.interceptor
}

moved {
  from = aws_iam_role_policy.interceptor[0]
  to   = aws_iam_role_policy.interceptor
}

moved {
  from = aws_lambda_function.interceptor[0]
  to   = aws_lambda_function.interceptor
}
