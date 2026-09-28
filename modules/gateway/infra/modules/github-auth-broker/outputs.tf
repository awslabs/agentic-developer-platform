# =============================================================================
# GitHub Auth Broker — Outputs
# Issue #520, Issue #525, Issue #1011
# =============================================================================

output "function_name" {
  description = "Name of the GitHub auth broker Lambda function"
  value       = aws_lambda_function.broker.function_name
}

output "function_arn" {
  description = "ARN of the GitHub auth broker Lambda function"
  value       = aws_lambda_function.broker.arn
}

output "invoke_arn" {
  description = "Invoke ARN of the GitHub auth broker Lambda (for API Gateway integration)"
  value       = aws_lambda_function.broker.invoke_arn
}

output "role_arn" {
  description = "IAM role ARN for the broker Lambda"
  value       = aws_iam_role.broker.arn
}
