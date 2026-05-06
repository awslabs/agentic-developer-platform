# =============================================================================
# GitHub Auth Broker — Outputs
# Issue #520
# =============================================================================

output "function_name" {
  description = "Name of the GitHub auth broker Lambda function"
  value       = aws_lambda_function.broker.function_name
}

output "function_arn" {
  description = "ARN of the GitHub auth broker Lambda function"
  value       = aws_lambda_function.broker.arn
}

output "function_url" {
  description = "Public Function URL for the GitHub auth broker"
  value       = aws_lambda_function_url.broker.function_url
}

output "role_arn" {
  description = "IAM role ARN for the broker Lambda"
  value       = aws_iam_role.broker.arn
}
