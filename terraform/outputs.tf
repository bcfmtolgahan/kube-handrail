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
  description = "App client IDs by client name. Secrets are not output; read them from Cognito."
  value       = { for k, c in aws_cognito_user_pool_client.this : k => c.id }
}

output "plans_table_name" {
  description = "DynamoDB table holding plans."
  value       = aws_dynamodb_table.plans.name
}

output "kill_switch_parameter" {
  description = "SSM parameter that enables remediation when set to \"true\"."
  value       = aws_ssm_parameter.kill_switch.name
}

output "lambda_role_arns" {
  description = "IAM role ARNs of the Lambda functions, by function."
  value       = { for k, r in aws_iam_role.lambda : k => r.arn }
}

output "sentinel_function_name" {
  description = "Name of the drift sentinel Lambda, or null when it is disabled."
  value       = one(aws_lambda_function.sentinel[*].function_name)
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
