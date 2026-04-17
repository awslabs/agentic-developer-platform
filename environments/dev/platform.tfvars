environment = "dev"
aws_region  = "us-east-1"

vpc_cidr           = "10.0.0.0/16"
az_count           = 2
single_nat_gateway = true

eks_cluster_version     = "1.35"
eks_node_instance_types = ["m5.large", "m5.xlarge"]
eks_node_desired_size   = 2
eks_node_min_size       = 1
eks_node_max_size       = 10

# `eks_public_access_cidrs` is intentionally NOT set here so the repo stays
# portable. Set it per-invocation via:
#   export TF_VAR_eks_public_access_cidrs='["<your.public.ip>/32"]'
# The deploy-all.sh and preflight-check.sh scripts autodetect the operator's IP
# when this variable is unset.
