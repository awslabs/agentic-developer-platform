environment = "dev"
aws_region  = "us-east-1"
account_id  = "ACCOUNT_ID"

# VPC peering — resolved automatically from platform remote state.
# Override variables below only if you need to peer with a non-standard VPC.
# Leave empty/default to use platform remote state outputs (see main.tf).
adp_vpc_id                  = ""
adp_private_route_table_ids = []
adp_eks_security_group_id   = ""
