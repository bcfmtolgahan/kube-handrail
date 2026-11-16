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

# --- Dashboard ---------------------------------------------------------------
#
# One dashboard over the metrics the Lambdas already emit: tool calls per plan and apply
# tool, guard rejections by code, tool errors, the sentinel's drift and unknown results,
# the verifier's runs, backlog and reverts, and the audit record's dead letters and lag.
# It follows sentinel_enabled like the alarms; the audit widgets follow audit_enabled.

locals {
  dashboard_tools = flatten([for fn in values(local.functions) : [for tool in fn.tools : tool.name]])
  plan_tools      = [for name in local.dashboard_tools : name if startswith(name, "plan_")]
  apply_tools     = [for name in local.dashboard_tools : name if startswith(name, "apply_")]

  # One JSON string per widget, so the optional groups have one type (list of strings).
  dashboard_widgets = concat(
    [
      jsonencode({
        title   = "Plans (calls, including refused)"
        metrics = [for name in local.plan_tools : [local.tools_metric_namespace, "ToolCalls", "Tool", name]]
      }),
      jsonencode({
        title   = "Applies (calls, including refused; approvals apply through the same tools)"
        metrics = [for name in local.apply_tools : [local.tools_metric_namespace, "ToolCalls", "Tool", name]]
      }),
      jsonencode({
        title = "Guard rejections by code"
        metrics = [[{
          id         = "rejections"
          expression = "SEARCH('{\"${local.tools_metric_namespace}\",Code,Tool} MetricName=\"GuardRejections\"', 'Sum', 300)"
        }]]
      }),
      jsonencode({
        title   = "Tool errors (internal, kubernetes_error, admission_denied)"
        metrics = [[local.tools_metric_namespace, "ToolErrors"]]
      }),
      jsonencode({
        title = "Drift sentinel (any value above 0 turned remediation off or could not check)"
        metrics = [
          ["${var.name_prefix}/sentinel", "Drift"],
          ["${var.name_prefix}/sentinel", "Unknown"],
        ]
      }),
    ],
    [
      jsonencode({
        title = "Verifier: runs and backlog (Due stays above Processed while it falls behind)"
        metrics = [
          [local.verifier_metric_namespace, "Runs"],
          [local.verifier_metric_namespace, "Due"],
          [local.verifier_metric_namespace, "Processed"],
          [local.verifier_metric_namespace, "InvokeErrors"],
        ]
      }),
      jsonencode({
        title = "Verifier: degraded changes and automatic reverts"
        metrics = [
          [local.verifier_metric_namespace, "Degraded"],
          [local.verifier_metric_namespace, "Reverts"],
        ]
      }),
    ],
    var.audit_enabled ? [
      jsonencode({
        title   = "Audit record: dead letters (records given up on)"
        metrics = [["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.audit_dlq[0].name, { stat = "Maximum" }]]
      }),
      jsonencode({
        title = "Audit record: write failures and stream lag (ms)"
        metrics = [
          [local.audit_metric_namespace, "Failures"],
          ["AWS/Lambda", "IteratorAge", "FunctionName", aws_lambda_function.audit[0].function_name, { stat = "Maximum", yAxis = "right" }],
        ]
      }),
    ] : [],
  )
}

resource "aws_cloudwatch_dashboard" "this" {
  count = local.sentinel_count

  dashboard_name = var.name_prefix
  dashboard_body = jsonencode({
    widgets = [
      for i, widget in local.dashboard_widgets : {
        type   = "metric"
        x      = (i % 2) * 12
        y      = floor(i / 2) * 6
        width  = 12
        height = 6
        properties = merge(
          { region = local.region, view = "timeSeries", stacked = false, period = 300, stat = "Sum" },
          jsondecode(widget),
        )
      }
    ]
  })
}
