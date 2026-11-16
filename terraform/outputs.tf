output "gateway_url" {
  description = "MCP endpoint of the AgentCore gateway."
  value       = aws_bedrockagentcore_gateway.this.gateway_url
}

output "cognito_token_endpoint" {
  description = "OAuth 2.0 token endpoint for the client credentials flow."
  value       = "https://${aws_cognito_user_pool_domain.this.domain}.auth.${local.region}.amazoncognito.com/oauth2/token"
}

output "cognito_scopes" {
  description = "Scopes to request, space-separated in the token request."
  value       = [for s in local.scopes : "${aws_cognito_resource_server.this.identifier}/${s}"]
}

output "cognito_client_ids" {
  description = "IDs of the app clients created here, by agent name. Secrets are not output; read them from Cognito."
  value       = { for k, c in aws_cognito_user_pool_client.this : k => c.id }
}

output "agent_client_ids" {
  description = "OAuth client ID of every agent, created or external, by agent name."
  value       = local.agent_client_ids
}

output "agent_kill_switch_parameters" {
  description = "Per-agent kill switch parameters by agent name; only the value \"true\" lets the agent act."
  value       = { for name, p in aws_ssm_parameter.agent_enabled : name => p.name }
}

output "plan_ttl_seconds" {
  description = "Seconds a plan stays valid (PLAN_TTL_SECONDS): plan_ttl_seconds, or the approval_mode default when it is null."
  value       = local.plan_ttl
}

output "guard_settings" {
  description = "The guard settings the tool Lambdas run with, for tests (tests/e2e/e2e_gateway.py reads them) and operators. No secrets."
  value = {
    name_prefix                = var.name_prefix
    approval_mode              = var.approval_mode
    plan_ttl_seconds           = local.plan_ttl
    apply_cooldown_seconds     = var.apply_cooldown_seconds
    flap_window_seconds        = var.flap_window_seconds
    object_hourly_apply_budget = var.object_hourly_apply_budget
    global_hourly_apply_budget = var.global_hourly_apply_budget
    agent_hourly_apply_budget  = var.agent_hourly_apply_budget
    agent_budgets              = { for name, agent in var.agents : name => coalesce(agent.hourly_apply_budget, var.agent_hourly_apply_budget) }
    min_replicas               = var.min_replicas
    max_replicas               = var.max_replicas
    max_replica_step_percent   = var.max_replica_step_percent
    verify_enabled             = var.verify_enabled
    verify_window_min_seconds  = var.verify_window_min_seconds
    gitops_markers             = var.gitops_markers
  }
}

output "plans_table_name" {
  description = "DynamoDB table holding plans."
  value       = aws_dynamodb_table.plans.name
}

output "kill_switch_parameter" {
  description = "SSM parameter that enables remediation when set to \"true\"."
  value       = aws_ssm_parameter.kill_switch.name
}

output "freezes_parameter" {
  description = "SSM parameter holding the change freezes; write it with scripts/freeze.py."
  value       = aws_ssm_parameter.freezes.name
}

output "lambda_role_arns" {
  description = "IAM role ARNs of the Lambda functions, by function."
  value       = { for k, r in aws_iam_role.lambda : k => r.arn }
}

output "sentinel_function_name" {
  description = "Name of the drift sentinel Lambda, or null when it is disabled."
  value       = one(aws_lambda_function.sentinel[*].function_name)
}

output "verifier_function_name" {
  description = "Name of the verifier Lambda, or null when verification is disabled."
  value       = one(aws_lambda_function.verifier[*].function_name)
}

output "alert_topic_arn" {
  description = "SNS topic for drift alerts and sentinel alarms, or null when the sentinel is disabled."
  value       = one(aws_sns_topic.alerts[*].arn)
}

output "approval_url" {
  description = "Approval API (Function URL, AWS_IAM auth) for scripts/approve.py (APPROVAL_URL)."
  value       = aws_lambda_function_url.approver.function_url
}

output "approval_topic_arn" {
  description = "SNS topic that request_approval notifies."
  value       = aws_sns_topic.approvals.arn
}

output "approver_policy_arn" {
  description = "IAM policy granting access to the approval URL (with MFA when approval_require_mfa). Attach it to each approver when approval_require_mfa is true."
  value       = aws_iam_policy.approver_invoke.arn
}

output "lambda_security_group_ids" {
  description = "Security groups of the tool Lambdas, created or given; empty when they run outside a VPC. Allow inbound 443 from these on the cluster's API server."
  value       = local.lambda_security_group_ids
}

output "audit_bucket_name" {
  description = "S3 bucket with the audit record (scripts/audit.py, AUDIT_BUCKET), or null when audit_enabled is false."
  value       = one(aws_s3_bucket.audit[*].bucket)
}

output "audit_dlq_url" {
  description = "SQS queue that receives plan stream records the audit Lambda gave up on, or null when audit_enabled is false."
  value       = one(aws_sqs_queue.audit_dlq[*].url)
}

output "dashboard_name" {
  description = "CloudWatch dashboard of the tools, the sentinel, the verifier and the audit record, or null when the sentinel is disabled."
  value       = one(aws_cloudwatch_dashboard.this[*].dashboard_name)
}
