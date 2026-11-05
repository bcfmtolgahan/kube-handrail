# --- Drift sentinel (mcp/CONTRACT.md, Drift sentinel) -------------------------
#
# A scheduled Lambda that compares the gateway targets and, when configured, the
# DevOps Agent association with mcp/classification.json, and checks that the gateway
# keeps its caller interceptor and allows only the agents' clients. On drift it turns
# the kill switch off and alerts; it never turns it back on, and never writes the
# per-agent kill switches.

locals {
  sentinel_count = var.sentinel_enabled ? 1 : 0
  sentinel_name  = "${var.name_prefix}-sentinel"

  classification = jsondecode(file("${path.module}/../mcp/classification.json"))
  deployed_tools = toset(flatten([
    for fn in local.functions : [for t in fn.tools : "${fn.target}___${t.name}"]
  ]))

  devops_agent_checks = var.devops_agent_space_id != "" && var.devops_agent_association_id != ""
  agent_space_arn     = "arn:${local.partition}:aidevops:${local.region}:${local.account_id}:agentspace/${var.devops_agent_space_id}"

  metric_namespace = "${var.name_prefix}/sentinel"
}

resource "aws_sns_topic" "alerts" {
  count = local.sentinel_count

  name = "${var.name_prefix}-alerts"
}

resource "aws_sns_topic_subscription" "alert_email" {
  count = var.sentinel_enabled && var.alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_iam_role" "sentinel" {
  count = local.sentinel_count

  name               = local.sentinel_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_cloudwatch_log_group" "sentinel" {
  count = local.sentinel_count

  name              = "/aws/lambda/${local.sentinel_name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "sentinel" {
  count = local.sentinel_count

  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.sentinel[0].arn}:*"]
  }

  statement {
    sid       = "KillSwitch"
    actions   = ["ssm:GetParameter", "ssm:PutParameter"]
    resources = [aws_ssm_parameter.kill_switch.arn]
  }

  statement {
    sid       = "Alert"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts[0].arn]
  }

  statement {
    sid       = "GatewayTargets"
    actions   = ["bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore:GetGatewayTarget"]
    resources = [aws_bedrockagentcore_gateway.this.gateway_arn]
  }

  statement {
    sid       = "GatewayConfiguration"
    actions   = ["bedrock-agentcore:GetGateway"]
    resources = [aws_bedrockagentcore_gateway.this.gateway_arn]
  }

  dynamic "statement" {
    for_each = local.devops_agent_checks ? [1] : []
    content {
      sid     = "DevOpsAgentRead"
      actions = ["aidevops:GetAgentSpace", "aidevops:GetAssociation"]
      resources = [
        local.agent_space_arn,
        "${local.agent_space_arn}/association/${var.devops_agent_association_id}",
      ]
    }
  }
}

resource "aws_iam_role_policy" "sentinel" {
  count = local.sentinel_count

  name   = "runtime"
  role   = aws_iam_role.sentinel[0].id
  policy = data.aws_iam_policy_document.sentinel[0].json
}

resource "aws_lambda_function" "sentinel" {
  count = local.sentinel_count

  function_name    = local.sentinel_name
  role             = aws_iam_role.sentinel[0].arn
  runtime          = local.runtime
  architectures    = [local.architecture]
  handler          = local.handler
  timeout          = 60
  memory_size      = 256
  filename         = data.archive_file.lambda["sentinel"].output_path
  source_code_hash = data.archive_file.lambda["sentinel"].output_base64sha256

  environment {
    variables = {
      GATEWAY_ID                = aws_bedrockagentcore_gateway.this.gateway_id
      REMEDIATION_ENABLED_PARAM = aws_ssm_parameter.kill_switch.name
      ALERT_TOPIC_ARN           = aws_sns_topic.alerts[0].arn
      METRIC_NAMESPACE          = local.metric_namespace
      TOOL_CLASSIFICATION       = jsonencode(local.classification)
      AGENT_SPACE_ID            = local.devops_agent_checks ? var.devops_agent_space_id : ""
      ASSOCIATION_ID            = local.devops_agent_checks ? var.devops_agent_association_id : ""
      EXPECT_ELEVATED_ACTIONS   = tostring(var.expect_elevated_actions)
      EXPECT_INTERCEPTOR_ARN    = var.caller_interceptor_enabled ? aws_lambda_function.interceptor[0].arn : ""
      EXPECT_ALLOWED_CLIENTS    = jsonencode(local.allowed_clients)
    }
  }

  # Updated after the targets, so a deploy that changes the tool set does not race its own check.
  depends_on = [
    aws_cloudwatch_log_group.sentinel,
    aws_iam_role_policy.sentinel,
    aws_bedrockagentcore_gateway_target.this,
  ]

  lifecycle {
    precondition {
      condition     = toset(keys(local.classification)) == local.deployed_tools
      error_message = "mcp/classification.json must classify exactly the tools in the targets' tools.json files."
    }
    precondition {
      condition     = (var.devops_agent_space_id == "") == (var.devops_agent_association_id == "")
      error_message = "Set both devops_agent_space_id and devops_agent_association_id, or neither."
    }
  }
}

resource "aws_cloudwatch_event_rule" "sentinel" {
  count = local.sentinel_count

  name                = local.sentinel_name
  description         = "Runs the kube-handrail drift sentinel."
  schedule_expression = var.sentinel_schedule
}

resource "aws_cloudwatch_event_target" "sentinel" {
  count = local.sentinel_count

  rule = aws_cloudwatch_event_rule.sentinel[0].name
  arn  = aws_lambda_function.sentinel[0].arn
}

resource "aws_lambda_permission" "sentinel" {
  count = local.sentinel_count

  statement_id  = "AllowSchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.sentinel[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sentinel[0].arn
}

# Unknown results are not drift and never touch the kill switch, but repeated ones, or no
# runs at all, mean the sentinel is blind. Assumes a schedule of at most 5 minutes.
resource "aws_cloudwatch_metric_alarm" "sentinel_unknown" {
  count = local.sentinel_count

  alarm_name          = "${local.sentinel_name}-unknown"
  alarm_description   = "The drift sentinel could not determine the tool classification state for 3 consecutive periods."
  namespace           = local.metric_namespace
  metric_name         = "Unknown"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  # Every run emits Unknown (0 or 1), so no data means the sentinel is not running at all.
  treat_missing_data = "breaching"
  alarm_actions      = [aws_sns_topic.alerts[0].arn]
  ok_actions         = [aws_sns_topic.alerts[0].arn]
}

# Invocation errors: the sentinel crashed, timed out, or could not complete its response to drift.
resource "aws_cloudwatch_metric_alarm" "sentinel_errors" {
  count = local.sentinel_count

  alarm_name          = "${local.sentinel_name}-errors"
  alarm_description   = "The drift sentinel failed, or could not turn the kill switch off or publish its alert."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.sentinel[0].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
}
