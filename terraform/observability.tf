# --- Tool records and metrics (mcp/CONTRACT.md, Observability) ----------------
#
# Every tool invocation logs one JSON line that is also a CloudWatch embedded metric
# record in this namespace. The alarms notify the alert topic, which the drift
# sentinel creates, so they follow sentinel_enabled.

locals {
  tools_metric_namespace = "${var.name_prefix}/tools"
}

# ToolErrors counts internal and kubernetes_error outcomes; guard rejections are
# GuardRejections.
resource "aws_cloudwatch_metric_alarm" "tool_errors" {
  count = local.sentinel_count

  alarm_name          = "${var.name_prefix}-tool-errors"
  alarm_description   = "A kube-handrail tool returned internal or kubernetes_error. Find the record with that outcome in the tool Lambdas' logs."
  namespace           = local.tools_metric_namespace
  metric_name         = "ToolErrors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
  ok_actions          = [aws_sns_topic.alerts[0].arn]
}

# Invocation errors: a tool Lambda timed out, ran out of memory or failed to start. Such an
# invocation prints no tool record, so ToolErrors cannot see it.
resource "aws_cloudwatch_metric_alarm" "tool_lambda_errors" {
  for_each = var.sentinel_enabled ? local.functions : {}

  alarm_name          = "${aws_lambda_function.this[each.key].function_name}-errors"
  alarm_description   = "A kube-handrail tool Lambda invocation failed (timeout, out of memory or start-up error) without a tool record."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.this[each.key].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
  ok_actions          = [aws_sns_topic.alerts[0].arn]
}
