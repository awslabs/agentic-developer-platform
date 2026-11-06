mock_provider "aws" {
  mock_data "aws_vpc" {
    defaults = {
      cidr_block_associations = [{
        association_id = "vpc-cidr-assoc-00000000000000001"
        cidr_block     = "172.20.0.0/16"
        state          = "associated"
      }]
    }
  }
  mock_data "aws_availability_zone" {
    defaults = { state = "available" }
  }
  mock_data "aws_default_tags" {
    defaults = { tags = {} }
  }
}

override_resource {
  target = aws_subnet.knowledge["first"]
  values = { id = "subnet-00000000000000001" }
}
override_resource {
  target = aws_subnet.knowledge["second"]
  values = { id = "subnet-00000000000000002" }
}
override_resource {
  target = aws_route_table.knowledge[0]
  values = { id = "rtb-00000000000000001" }
}

variables {
  enabled     = true
  name_prefix = "fixture-adp"
  vpc_id      = "vpc-00000000000000001"
  subnets = {
    first  = { availability_zone_id = "use1-az1", cidr_block = "172.20.40.0/24" }
    second = { availability_zone_id = "use1-az2", cidr_block = "172.20.41.0/24" }
  }
}

run "capacity_is_dormant_by_default" {
  command = plan
  variables { enabled = false }
  assert {
    condition = (
      length(aws_subnet.knowledge) == 0 && length(aws_route_table.knowledge) == 0 &&
      length(aws_route_table_association.knowledge) == 0 && length(data.aws_vpc.selected) == 0 &&
      length(data.aws_availability_zone.selected) == 0 && length(data.aws_default_tags.provider) == 0 &&
      output.managed_network == null && output.route_table_id == null
    )
    error_message = "Capacity inputs alone must neither read the VPC nor create infrastructure."
  }
}

run "isolated_private_capacity_has_only_local_routes" {
  command = apply
  assert {
    condition = (
      length(aws_subnet.knowledge) == 2 && length(aws_route_table.knowledge) == 1 &&
      length(aws_route_table_association.knowledge) == 2 &&
      alltrue([for subnet in aws_subnet.knowledge :
        subnet.vpc_id == "vpc-00000000000000001" && !subnet.map_public_ip_on_launch &&
        !subnet.assign_ipv6_address_on_creation && !subnet.enable_dns64 &&
        subnet.private_dns_hostname_type_on_launch == "ip-name" &&
        toset(keys(subnet.tags)) == toset(["Name", "Purpose"])
      ]) &&
      toset([for subnet in aws_subnet.knowledge : subnet.availability_zone_id]) == toset(["use1-az1", "use1-az2"]) &&
      toset([for subnet in aws_subnet.knowledge : subnet.cidr_block]) == toset(["172.20.40.0/24", "172.20.41.0/24"])
    )
    error_message = "Create only the two selected private IPv4 subnet ranges without workload discovery tags."
  }
  assert {
    condition = (
      aws_route_table.knowledge[0].vpc_id == "vpc-00000000000000001" &&
      length(aws_route_table.knowledge[0].route) == 0 &&
      toset([for association in aws_route_table_association.knowledge : association.subnet_id]) == toset(["subnet-00000000000000001", "subnet-00000000000000002"]) &&
      alltrue([for association in aws_route_table_association.knowledge : association.route_table_id == "rtb-00000000000000001"]) &&
      output.managed_network == { vpc_id = "vpc-00000000000000001", subnet_ids = ["subnet-00000000000000001", "subnet-00000000000000002"] } &&
      output.route_table_id == "rtb-00000000000000001"
    )
    error_message = "Both subnets must use the new local-only table and publish resolved inputs for the separate edge owner."
  }
}

run "refuse_same_availability_zone" {
  command = plan
  variables {
    subnets = {
      first  = { availability_zone_id = "use1-az1", cidr_block = "172.20.40.0/24" }
      second = { availability_zone_id = "use1-az1", cidr_block = "172.20.41.0/24" }
    }
  }
  expect_failures = [var.subnets]
}

run "refuse_duplicate_range" {
  command = plan
  variables {
    subnets = {
      first  = { availability_zone_id = "use1-az1", cidr_block = "172.20.40.0/24" }
      second = { availability_zone_id = "use1-az2", cidr_block = "172.20.40.0/24" }
    }
  }
  expect_failures = [var.subnets]
}

run "refuse_noncanonical_range" {
  command = plan
  variables {
    subnets = {
      first  = { availability_zone_id = "use1-az1", cidr_block = "172.20.40.1/24" }
      second = { availability_zone_id = "use1-az2", cidr_block = "172.20.41.0/24" }
    }
  }
  expect_failures = [var.subnets]
}

run "refuse_wrong_size_range" {
  command = plan
  variables {
    subnets = {
      first  = { availability_zone_id = "use1-az1", cidr_block = "172.20.40.0/25" }
      second = { availability_zone_id = "use1-az2", cidr_block = "172.20.41.0/24" }
    }
  }
  expect_failures = [var.subnets]
}

run "refuse_ipv6_range" {
  command = plan
  variables {
    subnets = {
      first  = { availability_zone_id = "use1-az1", cidr_block = "2001:db8::/24" }
      second = { availability_zone_id = "use1-az2", cidr_block = "172.20.41.0/24" }
    }
  }
  expect_failures = [var.subnets]
}

run "refuse_range_outside_selected_vpc" {
  command = plan
  variables {
    subnets = {
      first  = { availability_zone_id = "use1-az1", cidr_block = "172.20.40.0/24" }
      second = { availability_zone_id = "use1-az2", cidr_block = "172.21.41.0/24" }
    }
  }
  expect_failures = [aws_subnet.knowledge]
}

run "refuse_unassociated_vpc_range" {
  command = plan
  override_data {
    target = data.aws_vpc.selected[0]
    values = {
      cidr_block_associations = [{
        association_id = "vpc-cidr-assoc-00000000000000001", cidr_block = "172.20.0.0/16", state = "associating"
      }]
    }
  }
  expect_failures = [aws_subnet.knowledge]
}

run "refuse_unavailable_zone" {
  command = plan
  override_data {
    target = data.aws_availability_zone.selected["second"]
    values = { state = "unavailable" }
  }
  expect_failures = [aws_subnet.knowledge]
}

run "refuse_inherited_kubernetes_discovery" {
  command = plan
  override_data {
    target = data.aws_default_tags.provider[0]
    values = { tags = { "kubernetes.io/role/internal-elb" = "1" } }
  }
  expect_failures = [aws_subnet.knowledge]
}

run "refuse_inherited_karpenter_discovery" {
  command = plan
  override_data {
    target = data.aws_default_tags.provider[0]
    values = { tags = { "karpenter.sh/discovery" = "fixture" } }
  }
  expect_failures = [aws_subnet.knowledge]
}
