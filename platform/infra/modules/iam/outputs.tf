# EKS Cluster Role Outputs
output "eks_cluster_role_arn" {
  description = "ARN of the EKS cluster IAM role"
  value       = aws_iam_role.eks_cluster.arn
}

output "eks_cluster_role_name" {
  description = "Name of the EKS cluster IAM role"
  value       = aws_iam_role.eks_cluster.name
}

# EKS Node Group Role Outputs
output "eks_node_group_role_arn" {
  description = "ARN of the EKS node group IAM role"
  value       = aws_iam_role.eks_node_group.arn
}

output "eks_node_group_role_name" {
  description = "Name of the EKS node group IAM role"
  value       = aws_iam_role.eks_node_group.name
}

output "eks_node_group_instance_profile_name" {
  description = "Name of the EKS node group instance profile"
  value       = var.create_instance_profile ? aws_iam_instance_profile.eks_node_group[0].name : ""
}

# Gateway Service Role Outputs (placeholder)
output "gateway_service_role_arn" {
  description = "ARN of the gateway service IAM role (placeholder)"
  value       = aws_iam_role.gateway_service_placeholder.arn
}

output "gateway_service_role_name" {
  description = "Name of the gateway service IAM role (placeholder)"
  value       = aws_iam_role.gateway_service_placeholder.name
}

# Bedrock Pool Role Template Policies (for documentation)
output "bedrock_pool_trust_policy_template" {
  description = "Trust policy template for Bedrock pool roles in external accounts"
  value       = local.bedrock_pool_trust_policy
}

output "bedrock_pool_permissions_policy_template" {
  description = "Permissions policy template for Bedrock pool roles in external accounts"
  value       = local.bedrock_pool_permissions_policy
}