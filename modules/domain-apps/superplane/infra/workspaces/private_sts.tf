# IRSA requires private STS reachability before managed nodes can become ready.
# Owned networking creates the endpoint and exact node ingress. Supplied networking
# reads one existing endpoint; its owner retains its lifecycle and ingress policy.
resource "aws_security_group" "private_sts" {
  count = local.owns_network ? 1 : 0

  name        = "${local.name_prefix}-private-sts"
  description = "Private STS for this workspace's node security group only."
  vpc_id      = local.vpc_id
}

resource "aws_vpc_endpoint" "private_sts" {
  count = local.owns_network ? 1 : 0

  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.aws_region}.sts"
  vpc_endpoint_type   = "Interface"
  private_dns_enabled = true
  subnet_ids          = local.private_subnet_ids
  security_group_ids  = [aws_security_group.private_sts[0].id]

  tags = { Name = "${local.name_prefix}-private-sts" }
}

resource "aws_vpc_security_group_ingress_rule" "private_sts_nodes" {
  count = local.owns_network ? 1 : 0

  security_group_id            = aws_security_group.private_sts[0].id
  referenced_security_group_id = aws_eks_cluster.workspace.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  description                  = "This workspace's nodes may reach its private STS endpoint."
}

data "aws_vpc_endpoint" "supplied_sts" {
  count = local.owns_network ? 0 : 1

  vpc_id       = local.vpc_id
  service_name = "com.amazonaws.${var.aws_region}.sts"

  filter {
    name   = "vpc-endpoint-type"
    values = ["Interface"]
  }

  filter {
    name   = "vpc-endpoint-state"
    values = ["available"]
  }

  lifecycle {
    postcondition {
      condition     = self.private_dns_enabled && length(self.security_group_ids) == 1
      error_message = "Supplied private STS must enable private DNS and identify exactly one endpoint security group."
    }
  }
}

# The node SG is assigned by EKS during cluster creation. Read the supplied rule
# after that identity exists and fail before creating nodes if its owner has not
# enabled this exact path; bootstrap must not discover this after nodes stall.
data "aws_security_group" "supplied_sts" {
  count = local.owns_network ? 0 : 1

  id = one(data.aws_vpc_endpoint.supplied_sts[0].security_group_ids)

  depends_on = [aws_eks_cluster.workspace]

  lifecycle {
    postcondition {
      condition     = self.arn == "arn:${local.partition}:ec2:${var.aws_region}:${var.account_id}:security-group/${self.id}" && self.vpc_id == local.vpc_id && length(local.supplied_sts_rule_ids) == 1
      error_message = "Supplied private STS must already allow this EKS node security group on TCP 443. Its owner must configure ingress before node creation; workspace provisioning never mutates that external rule."
    }
  }
}

# Enumerate the existing endpoint group's rule IDs at plan time, then reread
# their exact provider fields after EKS creates its managed node SG. Neither
# data source can authorize a write or adopt these external rules into state.
data "aws_vpc_security_group_rules" "supplied_sts" {
  count = local.owns_network ? 0 : 1

  filter {
    name   = "group-id"
    values = [one(data.aws_vpc_endpoint.supplied_sts[0].security_group_ids)]
  }
}

data "aws_vpc_security_group_rule" "supplied_sts" {
  for_each = local.owns_network ? toset([]) : toset(data.aws_vpc_security_group_rules.supplied_sts[0].ids)

  security_group_rule_id = each.value
  depends_on             = [aws_eks_cluster.workspace]
}

locals {
  supplied_sts_rule_ids = [
    for rule in data.aws_vpc_security_group_rule.supplied_sts : rule.security_group_rule_id
    if !rule.is_egress && rule.ip_protocol == "tcp" && rule.from_port == 443 && rule.to_port == 443 && contains(flatten(data.aws_vpc_endpoint.supplied_sts[*].security_group_ids), rule.security_group_id) && rule.referenced_security_group_id == aws_eks_cluster.workspace.vpc_config[0].cluster_security_group_id
  ]
}

output "sts_endpoint_id" {
  value = local.owns_network ? aws_vpc_endpoint.private_sts[0].id : data.aws_vpc_endpoint.supplied_sts[0].id
}

output "sts_endpoint_vpc_id" {
  value = local.vpc_id
}

output "sts_endpoint_security_group_id" {
  value = local.owns_network ? aws_security_group.private_sts[0].id : one(data.aws_vpc_endpoint.supplied_sts[0].security_group_ids)
}

output "sts_endpoint_rule_id" {
  description = "Owned Terraform rule identity; supplied ingress remains externally owned."
  value       = local.owns_network ? aws_vpc_security_group_ingress_rule.private_sts_nodes[0].id : try(one(local.supplied_sts_rule_ids), null)
}
