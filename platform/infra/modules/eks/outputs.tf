# EKS Cluster Outputs
output "cluster_name" {
  description = "Name of the EKS cluster"
  value       = aws_eks_cluster.main.name
}

output "cluster_arn" {
  description = "ARN of the EKS cluster"
  value       = aws_eks_cluster.main.arn
}

output "cluster_endpoint" {
  description = "Endpoint for EKS control plane"
  value       = aws_eks_cluster.main.endpoint
}

output "cluster_version" {
  description = "The Kubernetes server version for the EKS cluster"
  value       = aws_eks_cluster.main.version
}

output "cluster_security_group_id" {
  description = "Security group IDs attached to the EKS cluster"
  value       = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
}

output "cluster_ca_certificate" {
  description = "Base64 encoded certificate data required to communicate with the cluster"
  value       = aws_eks_cluster.main.certificate_authority[0].data
}

output "cluster_oidc_issuer_url" {
  description = "The URL on the EKS cluster OIDC Issuer"
  value       = aws_eks_cluster.main.identity[0].oidc[0].issuer
}

output "cluster_primary_security_group_id" {
  description = "The cluster primary security group ID created by the EKS cluster"
  value       = aws_eks_cluster.main.vpc_config[0].cluster_security_group_id
}

# OIDC Provider
output "oidc_provider_arn" {
  description = "ARN of the OIDC provider"
  value       = aws_iam_openid_connect_provider.cluster.arn
}

output "oidc_issuer_url" {
  description = "OIDC issuer URL (without https:// prefix)"
  value       = replace(aws_eks_cluster.main.identity[0].oidc[0].issuer, "https://", "")
}

# Gateway Service IRSA Role
output "gateway_service_irsa_role_arn" {
  description = "ARN of the gateway service IRSA role (for Bedrock access)"
  value       = aws_iam_role.gateway_service_irsa.arn
}

output "gateway_service_irsa_role_name" {
  description = "Name of the gateway service IRSA role"
  value       = aws_iam_role.gateway_service_irsa.name
}

# Service Account Outputs
output "gateway_service_account_name" {
  description = "Name of the gateway Kubernetes service account"
  value       = kubernetes_service_account.gateway_service.metadata[0].name
}

output "gateway_service_account_namespace" {
  description = "Namespace of the gateway Kubernetes service account"
  value       = kubernetes_service_account.gateway_service.metadata[0].namespace
}
