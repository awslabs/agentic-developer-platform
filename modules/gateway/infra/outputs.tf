# VPC and Networking Outputs
output "vpc_id" {
  description = "ID of the VPC"
  value       = module.networking.vpc_id
}

output "private_subnet_ids" {
  description = "IDs of the private subnets"
  value       = module.networking.private_subnet_ids
}

output "public_subnet_ids" {
  description = "IDs of the public subnets"
  value       = module.networking.public_subnet_ids
}

# EKS Cluster Outputs
output "cluster_name" {
  description = "Name of the EKS cluster"
  value       = module.eks.cluster_name
}

output "cluster_endpoint" {
  description = "EKS cluster endpoint"
  value       = module.eks.cluster_endpoint
}

output "cluster_security_group_id" {
  description = "Security group ID attached to the EKS cluster"
  value       = module.eks.cluster_security_group_id
}

output "cluster_oidc_issuer_url" {
  description = "The URL on the EKS cluster OIDC Issuer"
  value       = module.eks.cluster_oidc_issuer_url
}

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

# Redis Outputs (conditional)
output "redis_endpoint" {
  description = "ElastiCache Redis endpoint"
  value       = var.enable_redis ? module.redis[0].cache_nodes : null
}

output "redis_port" {
  description = "ElastiCache Redis port"
  value       = var.enable_redis ? module.redis[0].port : null
}

# ALB is managed by EKS Ingress controller — no Terraform outputs
# The Ingress ALB DNS is resolved dynamically in the deploy workflows

# ECR Outputs
output "ecr_repository_url" {
  description = "URL of the ECR repository"
  value       = module.ecr.repository_url
}

output "ecr_repository_arn" {
  description = "ARN of the ECR repository"
  value       = module.ecr.repository_arn
}

# IAM Outputs
output "gateway_service_role_arn" {
  description = "ARN of the gateway service IAM role (placeholder — see gateway_service_irsa_role_arn)"
  value       = module.iam.gateway_service_role_arn
}

output "gateway_service_irsa_role_arn" {
  description = "ARN of the gateway service IRSA role (used by pods for Bedrock access)"
  value       = module.eks.gateway_service_irsa_role_arn
}

output "gateway_service_role_name" {
  description = "Name of the gateway service IAM role"
  value       = module.iam.gateway_service_role_name
}

output "eks_cluster_role_arn" {
  description = "ARN of the EKS cluster IAM role"
  value       = module.iam.eks_cluster_role_arn
}

output "eks_node_group_role_arn" {
  description = "ARN of the EKS node group IAM role"
  value       = module.iam.eks_node_group_role_arn
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
# These outputs expose API Gateway details when enable_api_gateway = true.
# The invoke URL is the alternate endpoint for long-running LLM requests.

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
# These outputs expose the Lambda authorizer and agent registry details
# when enable_api_gateway = true.

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
