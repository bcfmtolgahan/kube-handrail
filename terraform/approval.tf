# --- Server-side approval (mcp/CONTRACT.md, Server-side approval) -------------
#
# The approver Lambda is the human approval channel: a Function URL with AWS_IAM auth,
# outside the gateway, so no MCP client can reach it. request_approval (k8s_read)
# notifies the approval topic; the approver records the decision and, in server and
# both mode, runs the apply through the write Lambda.

locals {
  approver_name = "${var.name_prefix}-approver"

  # Built from the name, not taken from aws_lambda_function.this: the read Lambda's
  # environment references the approver's URL, so a reference back would be a cycle.
  # Must match the function_name of aws_lambda_function.this["k8s_write"].
  write_function_name = "${var.name_prefix}-k8s-write"
  write_function_arn  = "arn:${local.partition}:lambda:${local.region}:${local.account_id}:function:${local.write_function_name}"

  # Machine identities. The approver refuses them even if they are listed as approvers.
  machine_role_arns = concat(
    [for r in aws_iam_role.lambda : r.arn],
    aws_iam_role.sentinel[*].arn,
    [aws_iam_role.approver.arn],
  )

  # Plan attributes only the approval channel writes (mcp/CONTRACT.md, Plan record).
  approval_attributes = [
    "approved_by",
    "approved_at",
    "approved_args_hash",
    "approval_channel",
    "approval_expires_at",
    "rejected_by",
    "rejected_at",
    "reject_reason",
  ]

  # Statement IDs allow only [A-Za-z0-9_-]; ARNs do not fit.
  approver_statements = { for arn in var.approver_principals : substr(sha256(arn), 0, 16) => arn }
}

resource "aws_sns_topic" "approvals" {
  name = "${var.name_prefix}-approvals"
}

resource "aws_sns_topic_subscription" "approval_email" {
  count = var.approval_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.approvals.arn
  protocol  = "email"
  endpoint  = var.approval_email
}

resource "aws_iam_role" "approver" {
  name               = local.approver_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "approver" {
  name              = "/aws/lambda/${local.approver_name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "approver" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.approver.arn}:*"]
  }

  statement {
    sid       = "Plans"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.plans.arn]
  }

  statement {
    sid       = "ApplyAfterApproval"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.write_function_arn]
  }
}

resource "aws_iam_role_policy" "approver" {
  name   = "runtime"
  role   = aws_iam_role.approver.id
  policy = data.aws_iam_policy_document.approver.json
}

resource "aws_lambda_function" "approver" {
  function_name = local.approver_name
  role          = aws_iam_role.approver.arn
  runtime       = local.runtime
  architectures = [local.architecture]
  handler       = local.handler
  # Longer than the write Lambda's 30 s, which it waits for.
  timeout          = 60
  memory_size      = 256
  filename         = data.archive_file.lambda["approver"].output_path
  source_code_hash = data.archive_file.lambda["approver"].output_base64sha256

  environment {
    variables = {
      PLANS_TABLE                      = aws_dynamodb_table.plans.name
      WRITE_FUNCTION_NAME              = local.write_function_name
      APPROVER_PRINCIPALS              = join(",", var.approver_principals)
      DENIED_PRINCIPALS                = join(",", local.machine_role_arns)
      APPROVAL_MODE                    = var.approval_mode
      APPLY_AFTER_APPROVAL_TTL_SECONDS = tostring(var.apply_after_approval_ttl_seconds)
    }
  }

  depends_on = [aws_cloudwatch_log_group.approver, aws_iam_role_policy.approver]

  lifecycle {
    precondition {
      condition     = var.approval_mode == "client" || length(var.approver_principals) > 0
      error_message = "approval_mode server or both needs at least one approver_principals entry; otherwise nobody can approve."
    }
    precondition {
      condition     = length(setintersection(toset(var.approver_principals), toset(local.machine_role_arns))) == 0
      error_message = "approver_principals must not list the kube-handrail Lambda roles; machine identities can never approve."
    }
    precondition {
      condition     = var.approval_wait_ttl_seconds >= var.plan_ttl_seconds
      error_message = "approval_wait_ttl_seconds must be at least plan_ttl_seconds."
    }
  }
}

resource "aws_lambda_function_url" "approver" {
  function_name      = aws_lambda_function.approver.function_name
  authorization_type = "AWS_IAM"
}

# Invoking an AWS_IAM function URL takes lambda:InvokeFunctionUrl and lambda:InvokeFunction.
# Lambda resource policies cannot carry an MFA condition, so with approval_require_mfa the
# grant is this identity policy, which the operator attaches to each approver user or role,
# and the function's resource policy grants nobody (a same-account allow there would skip
# the MFA condition). Without MFA (test setups only) the resource policy grants the listed
# principals directly. Either way, the approver's own allowlist is the gate that counts:
# any same-account principal with broader Lambda rights (lambda:* or administrator access)
# can call the URL without MFA, and only the allowlist refuses it.
data "aws_iam_policy_document" "approver_invoke" {
  statement {
    sid       = "ApprovalUrl"
    actions   = ["lambda:InvokeFunctionUrl"]
    resources = [aws_lambda_function.approver.arn]
    condition {
      test     = "StringEquals"
      variable = "lambda:FunctionUrlAuthType"
      values   = ["AWS_IAM"]
    }
    dynamic "condition" {
      for_each = var.approval_require_mfa ? [1] : []
      content {
        test     = "Bool"
        variable = "aws:MultiFactorAuthPresent"
        values   = ["true"]
      }
    }
  }

  statement {
    sid       = "ApprovalUrlInvoke"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.approver.arn]
    condition {
      test     = "Bool"
      variable = "lambda:InvokedViaFunctionUrl"
      values   = ["true"]
    }
    dynamic "condition" {
      for_each = var.approval_require_mfa ? [1] : []
      content {
        test     = "Bool"
        variable = "aws:MultiFactorAuthPresent"
        values   = ["true"]
      }
    }
  }
}

resource "aws_iam_policy" "approver_invoke" {
  name        = "${local.approver_name}-invoke"
  description = "Lets a kube-handrail approver call the approval URL. Attach to approver users or roles."
  policy      = data.aws_iam_policy_document.approver_invoke.json
}

resource "aws_lambda_permission" "approver_url" {
  for_each = var.approval_require_mfa ? {} : local.approver_statements

  statement_id           = "ApproverUrl-${each.key}"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.approver.function_name
  principal              = each.value
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "approver_invoke" {
  for_each = var.approval_require_mfa ? {} : local.approver_statements

  statement_id             = "ApproverInvoke-${each.key}"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.approver.function_name
  principal                = each.value
  invoked_via_function_url = true
}
