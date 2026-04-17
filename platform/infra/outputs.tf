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
