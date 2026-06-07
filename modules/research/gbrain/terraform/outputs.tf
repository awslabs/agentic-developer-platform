output "mcp_endpoint" {
  description = "Internal MCP endpoint URL for gbrain"
  value       = "http://${module.fargate.service_endpoint}:3000"
}

output "rds_endpoint" {
  description = "RDS PostgreSQL endpoint"
  value       = module.rds.endpoint
}

output "ecr_repo_url" {
  description = "ECR repository URL for gbrain container image"
  value       = module.storage.ecr_repo_url
}

output "service_arn" {
  description = "ECS service ARN"
  value       = module.fargate.service_arn
}

output "cluster_arn" {
  description = "ECS cluster ARN"
  value       = module.fargate.cluster_arn
}

output "bucket_name" {
  description = "S3 bucket name for brain repository"
  value       = module.storage.bucket_name
}

output "mcp_token_secret_arn" {
  description = "Secrets Manager ARN for MCP bearer token"
  value       = aws_secretsmanager_secret.mcp_token.arn
}

output "db_credentials_secret_arn" {
  description = "Secrets Manager ARN for RDS credentials"
  value       = module.rds.credentials_secret_arn
}
