environment = "dev"
aws_region  = "us-east-1"
account_id  = "879318057152"

# VPC peering — populate after looking up the ADP VPC resources.
# Leave empty to skip peering (Phase 6 can be applied later).
adp_vpc_id                  = "vpc-0d6115bead9301d25"
adp_vpc_cidr                = "10.0.0.0/16"
adp_private_route_table_ids = ["rtb-0bb32777628bbfec7", "rtb-080bac192e8b76c1e"]
adp_eks_security_group_id   = "sg-0d08851d0139bb2eb"
