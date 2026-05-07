# =============================================================================
# GitHub Auth Broker — Outputs
# Issue #520, Issue #525
# =============================================================================

output "function_name" {
  description = "Name of the GitHub auth broker Lambda function"
  value       = aws_lambda_function.broker.function_name
}

output "function_arn" {
  description = "ARN of the GitHub auth broker Lambda function"
  value       = aws_lambda_function.broker.arn
}

output "role_arn" {
  description = "IAM role ARN for the broker Lambda"
  value       = aws_iam_role.broker.arn
}

output "api_resource_id" {
  description = "API Gateway resource ID for /auth/github/{proxy+}"
  value       = aws_api_gateway_resource.auth_github_proxy.id
}
