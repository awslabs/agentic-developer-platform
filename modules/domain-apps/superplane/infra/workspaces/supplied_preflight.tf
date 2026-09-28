# Supplied networks remain read-only. This module supports a reviewed zonal public-NAT
# egress path. Transit and endpoint-only designs require a separate supported contract.
# An explicit subnet association wins; otherwise the VPC main route table is effective.
data "aws_route_tables" "supplied_explicit" {
  for_each = data.aws_subnet.supplied
  vpc_id   = var.supplied_vpc_id
  filter {
    name   = "association.subnet-id"
    values = [each.key]
  }
}

data "aws_route_table" "supplied_main" {
  count  = local.owns_network ? 0 : 1
  vpc_id = var.supplied_vpc_id
  filter {
    name   = "association.main"
    values = ["true"]
  }
}

data "aws_route_table" "supplied_nodes" {
  for_each = data.aws_subnet.supplied
  route_table_id = length(data.aws_route_tables.supplied_explicit[each.key].ids) == 1 ? one(
    data.aws_route_tables.supplied_explicit[each.key].ids
  ) : data.aws_route_table.supplied_main[0].id
}

locals {
  supplied_nat_ids = toset(flatten([
    for table in data.aws_route_table.supplied_nodes : [
      for route in table.routes : route.nat_gateway_id
      if route.cidr_block == "0.0.0.0/0" && try(length(route.nat_gateway_id) > 0, false)
    ]
  ]))
  supplied_igw_ids = toset(flatten([
    for table in data.aws_route_table.supplied_nat : [
      for route in table.routes : route.gateway_id
      if route.cidr_block == "0.0.0.0/0" && try(startswith(route.gateway_id, "igw-"), false)
    ]
  ]))
}

data "aws_nat_gateway" "supplied" {
  for_each = local.supplied_nat_ids
  id       = each.key
}

data "aws_route_tables" "supplied_nat_explicit" {
  for_each = data.aws_nat_gateway.supplied
  vpc_id   = var.supplied_vpc_id
  filter {
    name   = "association.subnet-id"
    values = [each.value.subnet_id]
  }
}

data "aws_route_table" "supplied_nat" {
  for_each = data.aws_nat_gateway.supplied
  route_table_id = length(data.aws_route_tables.supplied_nat_explicit[each.key].ids) == 1 ? one(
    data.aws_route_tables.supplied_nat_explicit[each.key].ids
  ) : data.aws_route_table.supplied_main[0].id
}

data "aws_internet_gateway" "supplied" {
  for_each            = local.supplied_igw_ids
  internet_gateway_id = each.key
}
