output "vpc_id" {
  description = "VPC ID"
  value       = module.networking.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnet IDs"
  value       = module.networking.private_subnet_ids
}

output "public_subnet_ids" {
  description = "Public subnet IDs"
  value       = module.networking.public_subnet_ids
}

output "eks_cluster_name" {
  description = "EKS cluster name"
  value       = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  description = "EKS cluster endpoint"
  value       = module.eks.cluster_endpoint
}

output "eks_cluster_arn" {
  description = "EKS cluster ARN"
  value       = module.eks.cluster_arn
}

output "eks_oidc_issuer_url" {
  description = "EKS OIDC issuer URL (without https:// prefix)"
  value       = module.eks.oidc_issuer_url
}

# Alias — downstream modules (e.g. agent-factory) reference this name.
output "eks_oidc_issuer" {
  description = "Alias for eks_oidc_issuer_url (without https:// prefix)"
  value       = module.eks.oidc_issuer_url
}

output "eks_cluster_ca_certificate" {
  description = "Base64-encoded cluster CA certificate"
  value       = module.eks.cluster_ca_certificate
}

output "eks_oidc_provider_arn" {
  description = "EKS OIDC provider ARN"
  value       = module.eks.oidc_provider_arn
}

output "ecr_repository_urls" {
  description = "Map of ECR repository name to URL"
  value       = module.ecr.repository_urls
}

# ---------------------------------------------------------------------------
# Security Group outputs (used by gateway and other downstream modules)
# ---------------------------------------------------------------------------

output "vpc_cidr_block" {
  description = "CIDR block of the platform VPC"
  value       = module.networking.vpc_cidr_block
}

output "private_route_table_ids" {
  description = "Private route table IDs in the platform VPC"
  value       = module.networking.private_route_table_ids
}

output "eks_cluster_security_group_id" {
  description = "EKS Auto Mode cluster security group ID (auto-created by AWS)"
  value       = module.eks.cluster_security_group_id
}

output "eks_security_group_id" {
  description = "Terraform-managed EKS security group ID"
  value       = module.networking.eks_security_group_id
}

output "rds_security_group_id" {
  description = "Security group ID for RDS instances"
  value       = module.networking.rds_security_group_id
}

output "redis_security_group_id" {
  description = "Security group ID for ElastiCache Redis"
  value       = module.networking.redis_security_group_id
}

output "alb_security_group_id" {
  description = "Security group ID for ALB"
  value       = module.networking.alb_security_group_id
}

# ---------------------------------------------------------------------------
# Gateway Service IRSA role (created in platform EKS module)
# ---------------------------------------------------------------------------

output "gateway_service_irsa_role_arn" {
  description = "ARN of the gateway service IRSA role"
  value       = module.eks.gateway_service_irsa_role_arn
}

output "gateway_service_irsa_role_name" {
  description = "Name of the gateway service IRSA role"
  value       = module.eks.gateway_service_irsa_role_name
}

# ---------------------------------------------------------------------------
# CodeBuild outputs
# ---------------------------------------------------------------------------

output "codebuild_role_arn" {
  description = "ARN of the shared CodeBuild IAM role"
  value       = module.codebuild.codebuild_role_arn
}

output "codebuild_project_names" {
  description = "Map of logical key to CodeBuild project name"
  value       = module.codebuild.project_names
}

# ---------------------------------------------------------------------------
# KMS outputs (Issue #3789)
# ---------------------------------------------------------------------------

output "webhook_secrets_kms_key_arn" {
  description = "ARN of the shared webhook-secrets KMS key (consumed by gateway + webhook-ingress)"
  value       = aws_kms_key.webhook_secrets.arn
}

output "webhook_secrets_kms_alias_arn" {
  description = "ARN of the webhook-secrets KMS alias"
  value       = aws_kms_alias.webhook_secrets.arn
}

# ---------------------------------------------------------------------------
# Security Scans outputs
# ---------------------------------------------------------------------------

output "security_scans_bucket_arn" {
  description = "ARN of the security scans S3 bucket"
  value       = module.security_scans.bucket_arn
}

output "security_scans_bucket_name" {
  description = "Name of the security scans S3 bucket"
  value       = module.security_scans.bucket_name
}
