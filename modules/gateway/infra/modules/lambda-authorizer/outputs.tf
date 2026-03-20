# =============================================================================
# Outputs for Lambda Authorizer Module (Issue #239)
# =============================================================================

# =============================================================================
# Lambda Function
# =============================================================================

output "authorizer_lambda_arn" {
  description = "ARN of the Lambda authorizer function"
  value       = aws_lambda_function.authorizer.arn
}

output "authorizer_lambda_name" {
  description = "Name of the Lambda authorizer function"
  value       = aws_lambda_function.authorizer.function_name
}

output "authorizer_lambda_invoke_arn" {
  description = "Invoke ARN of the Lambda authorizer function"
  value       = aws_lambda_function.authorizer.invoke_arn
}

# =============================================================================
# API Gateway Authorizer
# =============================================================================

output "authorizer_id" {
  description = "ID of the API Gateway authorizer"
  value       = aws_api_gateway_authorizer.main.id
}

output "authorizer_name" {
  description = "Name of the API Gateway authorizer"
  value       = aws_api_gateway_authorizer.main.name
}

# =============================================================================
# DynamoDB Agent Registry
# =============================================================================

output "agent_registry_table_name" {
  description = "Name of the DynamoDB agent registry table"
  value       = aws_dynamodb_table.agent_registry.name
}

output "agent_registry_table_arn" {
  description = "ARN of the DynamoDB agent registry table"
  value       = aws_dynamodb_table.agent_registry.arn
}

# =============================================================================
# IAM Roles
# =============================================================================

output "authorizer_role_arn" {
  description = "ARN of the Lambda authorizer IAM role"
  value       = aws_iam_role.authorizer.arn
}

output "authorizer_invocation_role_arn" {
  description = "ARN of the API Gateway authorizer invocation role"
  value       = aws_iam_role.authorizer_invocation.arn
}

# =============================================================================
# CloudWatch Logs
# =============================================================================

output "log_group_name" {
  description = "Name of the CloudWatch log group for the authorizer"
  value       = aws_cloudwatch_log_group.authorizer.name
}

output "log_group_arn" {
  description = "ARN of the CloudWatch log group for the authorizer"
  value       = aws_cloudwatch_log_group.authorizer.arn
}
