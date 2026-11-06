terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "6.68.0" }
  }
}

variable "enabled" {
  type    = bool
  default = false
}

variable "name_prefix" { type = string }

variable "vpc_id" {
  type    = string
  default = ""
  validation {
    condition     = !var.enabled || can(regex("^vpc-[a-f0-9]{8,17}$", var.vpc_id))
    error_message = "Dedicated Knowledge capacity requires an explicit existing VPC."
  }
}

variable "subnets" {
  description = "Two dedicated /24 IPv4 ranges, selected from free space in the existing VPC, in distinct availability zones."
  type = map(object({
    availability_zone_id = string
    cidr_block           = string
  }))
  default = {}
  validation {
    condition = !var.enabled || (
      length(var.subnets) == 2 &&
      length(distinct([for subnet in var.subnets : subnet.availability_zone_id])) == 2 &&
      length(distinct([for subnet in var.subnets : subnet.cidr_block])) == 2 &&
      alltrue([for subnet in var.subnets : try(
        split("/", subnet.cidr_block)[1] == "24" &&
        cidrhost(subnet.cidr_block, 0) == split("/", subnet.cidr_block)[0] &&
        can(cidrnetmask(subnet.cidr_block)), false
      )])
    )
    error_message = "Select exactly two distinct, canonical IPv4 /24 ranges in two distinct availability zones."
  }
}

data "aws_vpc" "selected" {
  count = var.enabled ? 1 : 0
  id    = var.vpc_id
  # Select by status explicitly: the provider uses this as a lookup filter and
  # does not populate it from the VPC response when the input is omitted.
  state = "available"
}

data "aws_availability_zone" "selected" {
  for_each = var.enabled ? var.subnets : {}
  zone_id  = each.value.availability_zone_id
}

data "aws_default_tags" "provider" {
  count = var.enabled ? 1 : 0
}

resource "aws_subnet" "knowledge" {
  for_each                            = var.enabled ? var.subnets : {}
  vpc_id                              = var.vpc_id
  availability_zone_id                = each.value.availability_zone_id
  cidr_block                          = each.value.cidr_block
  map_public_ip_on_launch             = false
  assign_ipv6_address_on_creation     = false
  enable_dns64                        = false
  private_dns_hostname_type_on_launch = "ip-name"
  tags = {
    Name    = "${var.name_prefix}-knowledge-${each.key}"
    Purpose = "knowledge-private-capacity"
  }
  lifecycle {
    precondition {
      condition = (
        data.aws_vpc.selected[0].state == "available" &&
        anytrue([for block in data.aws_vpc.selected[0].cidr_block_associations : try(
          block.state == "associated" && tonumber(split("/", block.cidr_block)[1]) <= 24 &&
          cidrhost("${split("/", each.value.cidr_block)[0]}/${split("/", block.cidr_block)[1]}", 0) == cidrhost(block.cidr_block, 0), false
        )])
      )
      error_message = "Each Knowledge subnet must fit inside an associated IPv4 CIDR of the selected available VPC."
    }
    precondition {
      condition     = data.aws_availability_zone.selected[each.key].state == "available"
      error_message = "The selected Knowledge availability zones must be available in this region."
    }
    precondition {
      condition = alltrue([for key in keys(data.aws_default_tags.provider[0].tags) :
        !startswith(key, "kubernetes.io/") && !startswith(key, "karpenter.sh/") && !startswith(key, "eksctl.cluster.k8s.io/")
      ])
      error_message = "Use a provider without Kubernetes discovery default tags for dedicated Knowledge capacity."
    }
  }
}

# An explicit empty route set owns every nonlocal route. AWS supplies the VPC's
# immutable local route; no internet, NAT, peering or transit route is added.
resource "aws_route_table" "knowledge" {
  count  = var.enabled ? 1 : 0
  vpc_id = var.vpc_id
  route  = []
  tags = {
    Name    = "${var.name_prefix}-knowledge-local-only"
    Purpose = "knowledge-private-capacity"
  }
}

resource "aws_route_table_association" "knowledge" {
  for_each       = var.enabled ? var.subnets : {}
  subnet_id      = aws_subnet.knowledge[each.key].id
  route_table_id = aws_route_table.knowledge[0].id
}

output "managed_network" {
  description = "Resolved private inputs for a later, separately owned knowledge-edge plan."
  value = var.enabled ? {
    vpc_id     = var.vpc_id
    subnet_ids = [for key in sort(keys(var.subnets)) : aws_subnet.knowledge[key].id]
  } : null
  depends_on = [aws_route_table_association.knowledge]
}

output "route_table_id" {
  value = var.enabled ? aws_route_table.knowledge[0].id : null
}
