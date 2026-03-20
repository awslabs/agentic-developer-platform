# =============================================================================
# Outputs for API Gateway Module (Issue #236)
# =============================================================================

# =============================================================================
# API Gateway REST API
# =============================================================================

output "api_gateway_id" {
  description = "ID of the API Gateway REST API"
  value       = aws_api_gateway_rest_api.main.id
}

output "api_gateway_arn" {
  description = "ARN of the API Gateway REST API"
  value       = aws_api_gateway_rest_api.main.arn
}

output "api_gateway_name" {
  description = "Name of the API Gateway REST API"
  value       = aws_api_gateway_rest_api.main.name
}

output "api_gateway_root_resource_id" {
  description = "Root resource ID of the API Gateway REST API"
  value       = aws_api_gateway_rest_api.main.root_resource_id
}

# =============================================================================
# API Gateway Stage and Invoke URL
# =============================================================================

output "api_gateway_stage_name" {
  description = "Name of the API Gateway deployment stage"
  value       = aws_api_gateway_stage.main.stage_name
}

output "api_gateway_invoke_url" {
  description = "Invoke URL for the API Gateway stage (use this for API calls)"
  value       = aws_api_gateway_stage.main.invoke_url
}

output "api_gateway_execution_arn" {
  description = "Execution ARN of the API Gateway stage"
  value       = aws_api_gateway_stage.main.execution_arn
}

# =============================================================================
# VPC Link
# =============================================================================

output "vpc_link_id" {
  description = "ID of the VPC Link (empty if ALB ARN not provided)"
  value       = length(aws_api_gateway_vpc_link.main) > 0 ? aws_api_gateway_vpc_link.main[0].id : ""
}

output "vpc_link_arn" {
  description = "ARN of the VPC Link (empty if ALB ARN not provided)"
  value       = length(aws_api_gateway_vpc_link.main) > 0 ? aws_api_gateway_vpc_link.main[0].arn : ""
}

output "vpc_link_name" {
  description = "Name of the VPC Link"
  value       = length(aws_api_gateway_vpc_link.main) > 0 ? aws_api_gateway_vpc_link.main[0].name : ""
}

# =============================================================================
# CloudWatch Logging
# =============================================================================

output "access_log_group_name" {
  description = "Name of the CloudWatch log group for API Gateway access logs"
  value       = aws_cloudwatch_log_group.api_gateway.name
}

output "access_log_group_arn" {
  description = "ARN of the CloudWatch log group for API Gateway access logs"
  value       = aws_cloudwatch_log_group.api_gateway.arn
}

# =============================================================================
# Deployment
# =============================================================================

output "deployment_id" {
  description = "ID of the API Gateway deployment"
  value       = aws_api_gateway_deployment.main.id
}
