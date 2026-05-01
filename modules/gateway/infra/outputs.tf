# =============================================================================
# Gateway Module Outputs
# =============================================================================
# Only gateway-specific outputs. Platform-owned resources (VPC, EKS, ECR,
# CloudTrail, base IAM) are exposed via platform/infra/outputs.tf.
# =============================================================================

# RDS Outputs
output "rds_endpoint" {
  description = "RDS instance endpoint"
  value       = module.rds.db_instance_endpoint
}

output "rds_port" {
  description = "RDS instance port"
  value       = module.rds.db_instance_port
}

output "rds_database_name" {
  description = "RDS database name"
  value       = module.rds.db_instance_name
}

output "rds_master_user_secret_arn" {
  description = "ARN of the AWS-managed Secrets Manager secret holding the RDS master user password. Read just-in-time for one-time bootstrap DDL (e.g. GRANT rds_iam TO <user>); runtime uses IAM auth."
  value       = module.rds.master_user_secret_arn
}

# Redis Outputs (conditional)
output "redis_endpoint" {
  description = "ElastiCache Redis endpoint"
  value       = var.enable_redis ? module.redis[0].cache_nodes : null
}

output "redis_port" {
  description = "ElastiCache Redis port"
  value       = var.enable_redis ? module.redis[0].port : null
}

# Frontend Outputs
output "frontend_bucket_name" {
  description = "Name of the S3 bucket for frontend assets (for deploy workflow S3 sync)"
  value       = module.frontend_s3.bucket_name
}

output "frontend_cloudfront_distribution_id" {
  description = "ID of the CloudFront distribution (for cache invalidation)"
  value       = module.cloudfront.distribution_id
}

output "frontend_cloudfront_domain_name" {
  description = "Domain name of the CloudFront distribution (URL to access frontend)"
  value       = module.cloudfront.distribution_domain_name
}

# Environment Information
output "environment" {
  description = "Environment name"
  value       = var.environment
}

output "region" {
  description = "AWS region"
  value       = var.aws_region
}

# Cognito Outputs
output "cognito_user_pool_id" {
  description = "ID of the Cognito User Pool"
  value       = module.cognito.cognito_user_pool_id
}

output "cognito_user_pool_arn" {
  description = "ARN of the Cognito User Pool"
  value       = module.cognito.cognito_user_pool_arn
}

output "cognito_user_pool_client_id" {
  description = "ID of the Cognito User Pool Client"
  value       = module.cognito.cognito_user_pool_client_id
}

output "cognito_identity_pool_id" {
  description = "ID of the Cognito Identity Pool"
  value       = module.cognito.cognito_identity_pool_id
}

output "cognito_domain" {
  description = "Cognito User Pool domain"
  value       = module.cognito.cognito_domain
}

output "cognito_hosted_ui_url" {
  description = "URL for the Cognito Hosted UI"
  value       = module.cognito.cognito_hosted_ui_url
}

output "cognito_gateway_caller_role_arn" {
  description = "ARN of the gateway-caller IAM role for authenticated users"
  value       = module.cognito.gateway_caller_role_arn
}

# Agent Client Outputs (Issue #124)
output "cognito_agent_client_id" {
  description = "ID of the Cognito Agent App Client (for client_credentials flow)"
  value       = module.cognito.agent_client_id
}

output "cognito_agent_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret containing agent credentials"
  value       = module.cognito.agent_credentials_secret_arn
}

output "cognito_token_endpoint" {
  description = "OAuth2 token endpoint for client_credentials flow"
  value       = module.cognito.token_endpoint
}

# RDS Bootstrap Outputs (Issue #60)
output "rds_bootstrap_job_name" {
  description = "Name of the K8s Job that granted rds_iam to the DB user"
  value       = module.rds_bootstrap.bootstrap_job_name
}

# Cognito Test Users Outputs (Issue #60)
output "cognito_admins_group" {
  description = "Name of the Cognito admins group"
  value       = module.cognito.admins_group_name
}

output "test_user_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret for non-admin test user credentials"
  value       = module.cognito.test_user_credentials_secret_arn
}

output "test_admin_credentials_secret_arn" {
  description = "ARN of the Secrets Manager secret for admin test user credentials"
  value       = module.cognito.test_admin_credentials_secret_arn
}

# GitHub OAuth Identity Provider Outputs (Issue #313)
output "github_oauth_callback_url" {
  description = "OAuth callback URL to configure in the GitHub OAuth App settings"
  value       = module.cognito.github_oauth_callback_url
}

output "github_sign_in_url" {
  description = "Cognito hosted UI sign-in URL pre-selecting the GitHub identity provider"
  value       = module.cognito.github_sign_in_url
}

# Chat Logging Outputs (Issue #143)
output "chat_logs_bucket_name" {
  description = "Name of the S3 bucket for chat logs"
  value       = var.enable_chat_logging ? module.s3_chat_logs[0].bucket_name : ""
}

output "chat_logs_bucket_arn" {
  description = "ARN of the S3 bucket for chat logs"
  value       = var.enable_chat_logging ? module.s3_chat_logs[0].bucket_arn : ""
}

# CloudWatch Dashboard (Issue #144)
output "latency_dashboard_name" {
  description = "Name of the CloudWatch latency dashboard"
  value       = module.cloudwatch_dashboard.dashboard_name
}

# Budget Lambda Outputs (Issue #234)
output "budget_usage_tracker_lambda_arn" {
  description = "ARN of the budget usage tracker Lambda function"
  value       = var.enable_chat_logging ? module.budget_lambda[0].usage_tracker_lambda_arn : ""
}

output "budget_usage_tracker_lambda_name" {
  description = "Name of the budget usage tracker Lambda function"
  value       = var.enable_chat_logging ? module.budget_lambda[0].usage_tracker_lambda_name : ""
}

output "pricing_refresh_lambda_arn" {
  description = "ARN of the pricing refresh Lambda function"
  value       = var.enable_chat_logging ? module.budget_lambda[0].pricing_refresh_lambda_arn : ""
}

output "pricing_refresh_lambda_name" {
  description = "Name of the pricing refresh Lambda function"
  value       = var.enable_chat_logging ? module.budget_lambda[0].pricing_refresh_lambda_name : ""
}

# =============================================================================
# API Gateway Outputs (Issue #236)
# =============================================================================

output "api_gateway_invoke_url" {
  description = "Invoke URL for the API Gateway stage (alternate route with 15-min timeout)"
  value       = var.enable_api_gateway ? module.api_gateway[0].api_gateway_invoke_url : ""
}

output "api_gateway_id" {
  description = "ID of the API Gateway REST API"
  value       = var.enable_api_gateway ? module.api_gateway[0].api_gateway_id : ""
}

output "api_gateway_stage_name" {
  description = "Name of the API Gateway deployment stage"
  value       = var.enable_api_gateway ? module.api_gateway[0].api_gateway_stage_name : ""
}

output "api_gateway_vpc_link_id" {
  description = "ID of the API Gateway VPC Link"
  value       = var.enable_api_gateway ? module.api_gateway[0].vpc_link_id : ""
}

# =============================================================================
# Lambda Authorizer Outputs (Issue #239)
# =============================================================================

output "authorizer_lambda_invoke_arn" {
  description = "Invoke ARN of the Lambda authorizer function (used by API Gateway REQUEST authorizers)"
  value       = var.enable_api_gateway ? module.lambda_authorizer[0].authorizer_lambda_invoke_arn : ""
}

output "lambda_authorizer_arn" {
  description = "ARN of the Lambda authorizer function"
  value       = var.enable_api_gateway ? module.lambda_authorizer[0].authorizer_lambda_arn : ""
}

output "lambda_authorizer_name" {
  description = "Name of the Lambda authorizer function"
  value       = var.enable_api_gateway ? module.lambda_authorizer[0].authorizer_lambda_name : ""
}

output "api_gateway_authorizer_id" {
  description = "ID of the API Gateway authorizer"
  value       = var.enable_api_gateway ? module.lambda_authorizer[0].authorizer_id : ""
}

output "agent_registry_table_name" {
  description = "Name of the DynamoDB agent registry table"
  value       = var.enable_api_gateway ? module.lambda_authorizer[0].agent_registry_table_name : ""
}

output "agent_registry_table_arn" {
  description = "ARN of the DynamoDB agent registry table"
  value       = var.enable_api_gateway ? module.lambda_authorizer[0].agent_registry_table_arn : ""
}
