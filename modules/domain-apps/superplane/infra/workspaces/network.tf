# =============================================================================
# Workspace networking — owned, or supplied and only READ
# Issue #5532 (w6-09) design item 3.
# =============================================================================
# EVERY RESOURCE IN THIS FILE IS GATED ON `count = local.owns_network ? ... : 0`.
#
# That gating is the whole mechanism behind "support explicitly supplied networking without
# silently adopting its lifecycle". It is not a convention and not a comment: in `supplied`
# mode every count below evaluates to 0, so the plan contains no VPC, no subnet, no gateway
# and no route table, and `terraform destroy` for that workspace therefore cannot delete a
# network that was lent to ADP by its owner.
#
# The alternative shapes were considered and rejected:
#
#   * `terraform import` of the supplied VPC — puts it in this module's state, which is
#     exactly the adoption being avoided. A destroy then deletes the customer's network.
#   * One module with `create_vpc = true/false` on each resource's `count` but no separate
#     data path — leaves the cluster with no way to learn the supplied VPC's id.
#   * Two separate root modules — duplicates the cluster and IAM definitions, so a security
#     fix has to be made twice and the copy that is not in use silently rots.
#
# tests/supplied_networking.tftest.hcl plans the supplied mode and asserts the resource set;
# tests/test_networking_modes.py reads this file as text and asserts that EVERY
# `resource "aws_vpc|aws_subnet|aws_nat_gateway|aws_internet_gateway|aws_route_table|
# aws_route|aws_eip|aws_route_table_association"` declaration carries the gate. The text
# check exists because a future resource added here without the gate would be a silent
# regression that the plan test would only catch if someone remembered to extend it.
#
# Supplied topology and its private NAT egress are read and validated before planning:
# DNS, public-address assignment, effective node/NAT route tables and the attached IGW.
# No supplied object is managed. #5533 additionally verifies live node reachability,
# address headroom, NACLs, service access and bootstrap before marking the workspace usable.
# =============================================================================

# ---------------------------------------------------------------------------
# SUPPLIED MODE: read only.
#
# Read VPC/subnet topology and regional AZ availability without write permission or a
# state entry that a destroy can act on.
# ---------------------------------------------------------------------------
data "aws_vpc" "supplied" {
  count = local.owns_network ? 0 : 1

  id = var.supplied_vpc_id
}

# ---------------------------------------------------------------------------
# OWNED MODE: this module creates the network and may destroy it.
# ---------------------------------------------------------------------------
resource "aws_vpc" "workspace" {
  count = local.owns_network ? 1 : 0

  cidr_block = var.vpc_cidr

  # Both required by EKS: the cluster's private endpoint is resolved through VPC-provided
  # DNS, and without hostnames the endpoint's record does not resolve inside the VPC.
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = local.name_prefix
  }
}

# Flow logs are deliberately NOT declared here. They are a per-environment retention and
# cost decision with a destination (S3 or CloudWatch) this module does not own, and a flow
# log whose destination is created and destroyed with the workspace loses the record of the
# workspace's last hours — which is when it is most likely to be wanted. The control-plane
# log group in eks.tf is the audit surface this module does commit to.

resource "aws_internet_gateway" "workspace" {
  count = local.owns_network ? 1 : 0

  vpc_id = aws_vpc.workspace[0].id

  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

# Public subnets exist for egress (the NAT gateway) and for internet-facing load balancers
# the workspace's owner may later create. Workload capacity does NOT go here — the node
# group is placed in the private subnets only, see eks.tf.
resource "aws_subnet" "public" {
  count = local.owns_network ? length(var.availability_zones) : 0

  vpc_id            = aws_vpc.workspace[0].id
  availability_zone = var.availability_zones[count.index]

  # Deterministic carve of the VPC CIDR: /16 -> /20 subnets, public in the first half of
  # the address space and private in the second. Derived rather than an input, so two
  # workspaces with the same CIDR and zones produce the same layout and a plan diff never
  # shows a subnet moving because someone re-ordered a list.
  cidr_block = cidrsubnet(var.vpc_cidr, 4, count.index)

  # Public IPs are NOT auto-assigned. Anything that needs one asks for it explicitly; the
  # default-on behaviour is how an instance ends up internet-reachable without its author
  # choosing that.
  map_public_ip_on_launch = false

  tags = {
    Name = "${local.name_prefix}-public-${var.availability_zones[count.index]}"

    # Tells the AWS Load Balancer Controller which subnets may host an internet-facing
    # load balancer. Without it the controller cannot place one and reports a failure that
    # reads as a controller bug rather than a missing tag.
    "kubernetes.io/role/elb" = "1"
  }
}

resource "aws_subnet" "private" {
  count = local.owns_network ? length(var.availability_zones) : 0

  vpc_id            = aws_vpc.workspace[0].id
  availability_zone = var.availability_zones[count.index]

  # Offset by 8 so private subnets occupy the second half of a /16's /20s, leaving room to
  # add public subnets in more zones later without renumbering the private ones. A
  # renumber is a subnet REPLACEMENT, which means draining every node in it.
  cidr_block = cidrsubnet(var.vpc_cidr, 4, count.index + 8)

  tags = {
    Name                              = "${local.name_prefix}-private-${var.availability_zones[count.index]}"
    "kubernetes.io/role/internal-elb" = "1"
  }
}

# ---------------------------------------------------------------------------
# Egress: ONE NAT gateway, and that is a deliberate cost decision.
#
# One NAT gateway per availability zone is the resilient shape — a zone failure then does
# not take out egress for the surviving zones. One shared NAT gateway costs roughly a third
# as much at two zones and is what the vendored graph provisions
# (vendor/kro-account-factory/01-network-stack.yaml). This module matches the vendored graph
# so a workspace created through either path has the same bill and the same failure mode,
# and so design item 4's bounded estimate is a single figure rather than a per-zone one.
#
# The tradeoff is stated rather than hidden: if the zone holding this NAT gateway fails,
# every private subnet in the workspace loses outbound internet access until it returns.
# Pods already running continue; image pulls and outbound API calls do not.
# ---------------------------------------------------------------------------
resource "aws_eip" "nat" {
  count = local.owns_network ? 1 : 0

  domain = "vpc"

  tags = {
    Name = "${local.name_prefix}-nat"
  }

  # The EIP must exist before the gateway it is attached to, and the IGW must exist before
  # the EIP can be allocated in a VPC that has one.
  depends_on = [aws_internet_gateway.workspace]
}

resource "aws_nat_gateway" "workspace" {
  count = local.owns_network ? 1 : 0

  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id

  tags = {
    Name = "${local.name_prefix}-nat"
  }

  depends_on = [aws_internet_gateway.workspace]
}

resource "aws_route_table" "public" {
  count = local.owns_network ? 1 : 0

  vpc_id = aws_vpc.workspace[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.workspace[0].id
  }

  tags = {
    Name = "${local.name_prefix}-public"
  }
}

resource "aws_route_table" "private" {
  count = local.owns_network ? 1 : 0

  vpc_id = aws_vpc.workspace[0].id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.workspace[0].id
  }

  tags = {
    Name = "${local.name_prefix}-private"
  }
}

resource "aws_route_table_association" "public" {
  count = local.owns_network ? length(var.availability_zones) : 0

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

resource "aws_route_table_association" "private" {
  count = local.owns_network ? length(var.availability_zones) : 0

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[0].id
}

# ---------------------------------------------------------------------------
# Default Security Group — deny-all (CKV2_AWS_12)
# ---------------------------------------------------------------------------
# Adopts the VPC's default security group and removes all rules. This ensures
# nothing can accidentally rely on the permissive defaults AWS creates. All
# workloads use dedicated, scoped security groups (cluster SG, node SG, etc.).
#
# Gated on local.owns_network: in supplied mode this VPC belongs to the
# customer and adopting its default security group would revoke rules other
# workloads may depend on. The zero-count keeps it out of state entirely.

resource "aws_default_security_group" "workspace" {
  count = local.owns_network ? 1 : 0

  vpc_id = aws_vpc.workspace[0].id

  # Explicit empty sets keep both adoption and later drift reconciliation deny-all.
  # Omitting these Optional+Computed fields would leave rules state-derived.
  ingress = []
  egress  = []

  tags = {
    Name = "${local.name_prefix}-default-sg-restricted"
  }
}

data "aws_availability_zones" "selected" {
  state = "available"
  filter {
    name   = "zone-type"
    values = ["availability-zone"]
  }
}

data "aws_subnet" "supplied" {
  for_each = local.owns_network ? toset([]) : toset(var.supplied_private_subnet_ids)
  id       = each.value
}

resource "terraform_data" "topology_guard" {
  input = var.networking_mode
  lifecycle {
    precondition {
      condition = local.owns_network ? true : (
        data.aws_vpc.supplied[0].enable_dns_support && data.aws_vpc.supplied[0].enable_dns_hostnames
      )
      error_message = "Supplied VPC must enable DNS support and DNS hostnames for EKS nodes and its private endpoint."
    }
    precondition {
      condition     = alltrue([for subnet in data.aws_subnet.supplied : !subnet.map_public_ip_on_launch])
      error_message = "Supplied node subnets must disable automatic public IPv4 assignment."
    }
    precondition {
      condition = alltrue([
        for table in data.aws_route_table.supplied_nodes :
        table.vpc_id == var.supplied_vpc_id &&
        !anytrue([for route in table.routes : try(startswith(route.gateway_id, "igw-"), false)]) &&
        length([for route in table.routes : route if route.cidr_block == "0.0.0.0/0" && try(length(route.nat_gateway_id) > 0, false)]) == 1
      ])
      error_message = "Supplied node subnets must have no Internet Gateway route and exactly one IPv4 default route through a public NAT gateway. Transit and endpoint-only egress are not supported by this module."
    }
    precondition {
      condition = alltrue([
        for nat in data.aws_nat_gateway.supplied :
        nat.vpc_id == var.supplied_vpc_id && nat.state == "available" && nat.connectivity_type == "public" && try(length(nat.subnet_id) > 0, false)
      ])
      error_message = "Supplied node egress requires an available zonal public NAT gateway in the supplied VPC."
    }
    precondition {
      condition = alltrue([
        for table in data.aws_route_table.supplied_nat :
        table.vpc_id == var.supplied_vpc_id && length([for route in table.routes : route if route.cidr_block == "0.0.0.0/0" && try(startswith(route.gateway_id, "igw-"), false)]) == 1
        ]) && alltrue([
        for gateway in data.aws_internet_gateway.supplied : anytrue([
          for attachment in gateway.attachments : attachment.vpc_id == var.supplied_vpc_id
        ])
      ])
      error_message = "Supplied public NAT subnet must have a default Internet Gateway route with that gateway attached to the supplied VPC."
    }

    precondition {
      condition = !local.owns_network || alltrue([
        for zone in var.availability_zones : contains(data.aws_availability_zones.selected.names, zone)
      ])
      error_message = "Owned subnet zones must be available standard Availability Zones in the selected region."
    }
    precondition {
      condition = local.owns_network || alltrue([
        for subnet in data.aws_subnet.supplied : subnet.vpc_id == var.supplied_vpc_id && contains(data.aws_availability_zones.selected.names, subnet.availability_zone)
      ])
      error_message = "Supplied subnets must belong to supplied_vpc_id and the selected region's standard Availability Zones."
    }
    precondition {
      condition = local.owns_network || length(toset([
        for subnet in data.aws_subnet.supplied : subnet.availability_zone
      ])) >= 2
      error_message = "Supplied subnets must span at least two distinct Availability Zones."
    }
  }
}
