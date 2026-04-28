environment = "dev"
aws_region  = "us-east-1"
account_id  = "879318057152"

# VPC peering — populate after looking up the ADP VPC resources.
# Leave empty to skip peering (Phase 6 can be applied later).
adp_vpc_id                  = ""
adp_vpc_cidr                = "10.0.0.0/16"
adp_private_route_table_ids = []
adp_eks_security_group_id   = ""
