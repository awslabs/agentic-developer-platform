# Portable platform defaults. Installed settings take precedence during upgrades.
vpc_cidr                  = "10.0.0.0/16"
az_count                  = 2
single_nat_gateway        = true
eks_cluster_version       = "1.35"
eks_node_instance_types   = ["m5.large", "m5.xlarge"]
eks_node_desired_size     = 2
eks_node_min_size         = 1
eks_node_max_size         = 10
enable_container_insights = true
# Account-wide configuration is opt-in for portable installations. Upgrades
# recover existing ADP ownership from state, including partially applied logging.
manage_ecr_registry_scanning      = false
manage_bedrock_invocation_logging = false
# Network policy activation and legacy worker retirement require target evidence.
# New repositories use the module's KMS default; existing encryption is recovered.
