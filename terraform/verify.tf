# --- Verification and revert (mcp/CONTRACT.md, Verification and revert) ------
#
# An apply schedules its verification in the same write that consumes the plan. A scheduled
# Lambda outside the VPC reads the plans that are due from the verify-due index and runs
# each verification, and the revert of a degraded change approved with revert_on_failure,
# through the write Lambda's internal handler, which the gateway cannot reach. The verifier
# itself has no access to the cluster or the kill switch.

locals {
  verifier_name = "${var.name_prefix}-verifier"

  verifier_metric_namespace = "${var.name_prefix}/verifier"
  # The alarms notify the alert topic, which the drift sentinel creates, so they need both.
  verifier_alarm_count = var.sentinel_enabled ? 1 : 0

  # Written only by the write role: the schedule and the pinned revert targets (revert_to,
  # and revert_min_to and revert_max_to of an HPA adjust) by the consume, the rest by the
  # verifier channel. Denied to the read role with the approval
  # attributes, so a changed record cannot trigger or redirect a revert.
  verify_attributes = [
    "verify_at",
    "verify_queue",
    "verification",
    "revert",
    "revert_to",
    "revert_min_to",
    "revert_max_to",
    "kind",
    "reverts_plan_id",
  ]
}

resource "aws_iam_role" "verifier" {
  name               = local.verifier_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "verifier" {
  name              = "/aws/lambda/${local.verifier_name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "verifier" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.verifier.arn}:*"]
  }

  statement {
    sid       = "DuePlans"
    actions   = ["dynamodb:Query"]
    resources = ["${aws_dynamodb_table.plans.arn}/index/${local.verify_due_index}"]
  }

  statement {
    sid       = "Verify"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.write_function_arn]
  }
}

resource "aws_iam_role_policy" "verifier" {
  name   = "runtime"
  role   = aws_iam_role.verifier.id
  policy = data.aws_iam_policy_document.verifier.json
}

resource "aws_lambda_function" "verifier" {
  function_name = local.verifier_name
  role          = aws_iam_role.verifier.arn
  runtime       = local.runtime
  architectures = [local.architecture]
  handler       = local.handler
  # Up to VERIFY_BATCH_LIMIT synchronous invokes of the write Lambda, 30 s each at most.
  timeout          = 300
  memory_size      = 128
  filename         = data.archive_file.lambda["verifier"].output_path
  source_code_hash = data.archive_file.lambda["verifier"].output_base64sha256
  # Runs never overlap. Overlapping would be safe too: every write they cause is conditional.
  reserved_concurrent_executions = 1

  environment {
    variables = {
      PLANS_TABLE         = aws_dynamodb_table.plans.name
      WRITE_FUNCTION_NAME = local.write_function_name
      VERIFY_BATCH_LIMIT  = "10"
      METRIC_NAMESPACE    = local.verifier_metric_namespace
    }
  }

  depends_on = [aws_cloudwatch_log_group.verifier, aws_iam_role_policy.verifier]
}

# One minute is the shortest rate a scheduled rule supports.
resource "aws_cloudwatch_event_rule" "verifier" {
  name                = local.verifier_name
  description         = "Runs the kube-handrail verifier."
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "verifier" {
  rule = aws_cloudwatch_event_rule.verifier.name
  arn  = aws_lambda_function.verifier.arn
}

resource "aws_lambda_permission" "verifier" {
  statement_id  = "AllowSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.verifier.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.verifier.arn
}

# Every run emits Runs = 1, so no data means verifications are not running at all.
resource "aws_cloudwatch_metric_alarm" "verifier_stalled" {
  count = local.verifier_alarm_count

  alarm_name          = "${local.verifier_name}-stalled"
  alarm_description   = "The verifier has not run for 15 minutes; applied plans are not being verified, and late checks become unknown without a revert."
  namespace           = local.verifier_metric_namespace
  metric_name         = "Runs"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
  ok_actions          = [aws_sns_topic.alerts[0].arn]
}

# Invocation errors: the verifier crashed, timed out or failed to start.
resource "aws_cloudwatch_metric_alarm" "verifier_errors" {
  count = local.verifier_alarm_count

  alarm_name          = "${local.verifier_name}-errors"
  alarm_description   = "The verifier failed (timeout or start-up error). Plans it did not reach stay due and are retried every minute until their grace period ends."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.verifier.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
}

# Verifications the write Lambda could not run or returned an error for.
resource "aws_cloudwatch_metric_alarm" "verifier_invoke_errors" {
  count = local.verifier_alarm_count

  alarm_name          = "${local.verifier_name}-invoke-errors"
  alarm_description   = "A verification could not run through the write Lambda; find the run in the verifier's log. The plan stays due and is retried every minute until its grace period ends."
  namespace           = local.verifier_metric_namespace
  metric_name         = "InvokeErrors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
}

# A human hears about every automatic revert, whatever the agent reports.
resource "aws_cloudwatch_metric_alarm" "verifier_reverts" {
  count = local.verifier_alarm_count

  alarm_name          = "${local.verifier_name}-reverts"
  alarm_description   = "kube-handrail reverted a degraded change that was approved with revert_on_failure. Find the plan in the verifier's log (results) and read it with get_plan_outcome."
  namespace           = local.verifier_metric_namespace
  metric_name         = "Reverts"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
}

# The resources had count = 1 while verify_enabled was optional; existing deployments keep
# them under their new addresses. The alarms keep their count (sentinel_enabled).
moved {
  from = aws_iam_role.verifier[0]
  to   = aws_iam_role.verifier
}

moved {
  from = aws_cloudwatch_log_group.verifier[0]
  to   = aws_cloudwatch_log_group.verifier
}

moved {
  from = aws_iam_role_policy.verifier[0]
  to   = aws_iam_role_policy.verifier
}

moved {
  from = aws_lambda_function.verifier[0]
  to   = aws_lambda_function.verifier
}

moved {
  from = aws_cloudwatch_event_rule.verifier[0]
  to   = aws_cloudwatch_event_rule.verifier
}

moved {
  from = aws_cloudwatch_event_target.verifier[0]
  to   = aws_cloudwatch_event_target.verifier
}

moved {
  from = aws_lambda_permission.verifier[0]
  to   = aws_lambda_permission.verifier
}
