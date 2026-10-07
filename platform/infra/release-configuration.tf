# Operator inputs retained privately across portable release upgrades.
# Image selection remains owned by the release deployment.
output "release_configuration" {
  description = "Account-local deployment settings for subsequent release upgrades."
  sensitive   = true
  value = {
    manage_ecr_registry_scanning           = var.manage_ecr_registry_scanning
    manage_bedrock_invocation_logging      = var.manage_bedrock_invocation_logging
    bedrock_invocation_logging_enabled     = var.bedrock_invocation_logging_enabled
    agent_authority_legacy_workers_drained = var.agent_authority_legacy_workers_drained
    agent_legacy_worker_admin_retired      = var.agent_legacy_worker_admin_retired
    az_count                               = var.az_count
    ecr_repository_encryption              = var.ecr_repository_encryption
    eks_cluster_version                    = var.eks_cluster_version
    eks_node_desired_size                  = var.eks_node_desired_size
    eks_node_instance_types                = var.eks_node_instance_types
    eks_node_max_size                      = var.eks_node_max_size
    eks_node_min_size                      = var.eks_node_min_size
    enable_container_insights              = var.enable_container_insights
    enable_network_policy_controller       = var.enable_network_policy_controller
    single_nat_gateway                     = var.single_nat_gateway
    vpc_cidr                               = var.vpc_cidr
  }
}
