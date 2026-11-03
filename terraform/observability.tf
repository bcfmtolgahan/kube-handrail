# --- Tool records and metrics (mcp/CONTRACT.md, Observability) ----------------
#
# Every tool invocation logs one JSON line that is also a CloudWatch embedded metric
# record in this namespace. The alarm notifies the alert topic, which the drift
# sentinel creates, so it follows sentinel_enabled.

locals {
  tools_metric_namespace = "${var.name_prefix}/tools"
}

# ToolErrors counts internal errors only; guard rejections are GuardRejections.
resource "aws_cloudwatch_metric_alarm" "tool_errors" {
  count = local.sentinel_count

  alarm_name          = "${var.name_prefix}-tool-errors"
  alarm_description   = "A kube-handrail tool returned an internal error. Find the record with outcome internal in the tool Lambdas' logs."
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
