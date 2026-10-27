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
