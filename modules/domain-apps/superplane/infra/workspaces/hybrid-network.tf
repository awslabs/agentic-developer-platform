# EKS-side prerequisites for the SkyPilot/WireGuard hybrid node path.
# This configures the workspace cluster; it does not launch remote capacity,
# distribute activation credentials, or claim a WireGuard tunnel is healthy.
variable "hybrid_networks" {
  description = "Optional reviewed IPv4 ranges for hybrid nodes, hybrid pods and Kubernetes Services. Routing, CNI and HYBRID_LINUX access remain separate prerequisites."
  type = object({
    node_cidr    = string
    pod_cidr     = string
    service_cidr = string
  })
  default = null

  validation {
    condition = var.hybrid_networks == null ? true : try(tonumber(split("/", var.hybrid_networks.service_cidr)[1]) <= 24, false) && alltrue([
      for cidr in values(var.hybrid_networks) : try(
        can(cidrnetmask(cidr)) &&
        cidrhost(cidr, 0) == split("/", cidr)[0] &&
        tonumber(split("/", cidr)[1]) >= 16 &&
        tonumber(split("/", cidr)[1]) <= 28 &&
        can(regex("^(10\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|192\\.168\\.)", cidr)),
        false
      )
    ])
    error_message = "Hybrid ranges must be canonical RFC1918 IPv4 CIDRs: node/pod /16 through /28, service /16 through /24; broad routes such as 10.0.0.0/8 are not accepted."
  }
}

locals {
  hybrid_cidrs = var.hybrid_networks == null ? [] : values(var.hybrid_networks)
  # Include secondary VPC ranges in supplied mode, without adopting or editing
  # that VPC. The operator must choose nonoverlapping remote ranges.
  hybrid_vpc_cidrs = local.owns_network ? [var.vpc_cidr] : distinct(concat(
    [data.aws_vpc.supplied[0].cidr_block],
    [for association in data.aws_vpc.supplied[0].cidr_block_associations : association.cidr_block]
  ))
  # Terraform 1.9 has no cidrcontains function. Inclusive numeric intervals let
  # the actual plan reject both containment and partial overlap without shelling
  # out to a network discovery helper.
  hybrid_ranges = [for cidr in concat(local.hybrid_cidrs, local.hybrid_vpc_cidrs) : try({
    start = sum([for index, octet in split(".", cidrhost(cidr, 0)) : tonumber(octet) * pow(256, 3 - index)])
    end   = sum([for index, octet in split(".", cidrhost(cidr, -1)) : tonumber(octet) * pow(256, 3 - index)])
  }, null)]
  hybrid_ranges_disjoint = try(alltrue(flatten([
    for index, remote in slice(local.hybrid_ranges, 0, length(local.hybrid_cidrs)) : [
      for other_index, other in local.hybrid_ranges :
      other_index <= index || remote.end < other.start || other.end < remote.start
    ]
  ])), false)
}
