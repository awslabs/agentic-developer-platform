mock_provider "aws" {
  mock_data "aws_eks_cluster" {
    defaults = {
      vpc_config = [{ vpc_id = "vpc-00000000000000000", subnet_ids = ["subnet-00000000000000001"] }]
    }
  }
  mock_data "aws_subnet" {
    defaults = { availability_zone = "us-east-1a" }
  }
  mock_data "aws_route_table" {
    defaults = {
      id     = "rtb-00000000000000001"
      vpc_id = "vpc-00000000000000000"
      routes = [{ cidr_block = "0.0.0.0/0", nat_gateway_id = "nat-00000000000000001", gateway_id = "" }]
    }
  }
}
variables {
  account_id   = "123456789012"
  region       = "us-east-1"
  cluster_name = "example-eks-cluster"
  capacity_by_az = {
    "us-east-1a" = { cidr_block = "10.0.64.0/20", route_table_id = "rtb-00000000000000001" }
  }
}
run "private_capacity_uses_existing_cluster_vpc_and_routing" {
  command = plan
  assert {
    condition     = aws_subnet.capacity["us-east-1a"].vpc_id == "vpc-00000000000000000" && !aws_subnet.capacity["us-east-1a"].map_public_ip_on_launch && aws_route_table_association.capacity["us-east-1a"].route_table_id == "rtb-00000000000000001"
    error_message = "New capacity must stay private in the cluster VPC and reuse its private route table."
  }
}
run "public_routing_is_refused" {
  command = plan
  override_data {
    target = data.aws_route_table.private["us-east-1a"]
    values = {
      vpc_id = "vpc-00000000000000000"
      routes = [{ cidr_block = "0.0.0.0/0", nat_gateway_id = "", gateway_id = "igw-00000000000000001" }]
    }
  }
  expect_failures = [data.aws_route_table.private]
}
run "route_table_in_another_vpc_is_refused" {
  command = plan
  override_data {
    target = data.aws_route_table.private["us-east-1a"]
    values = {
      vpc_id = "vpc-00000000000000099"
      routes = [{ cidr_block = "0.0.0.0/0", nat_gateway_id = "nat-00000000000000001", gateway_id = "" }]
    }
  }
  expect_failures = [data.aws_route_table.private]
}
run "new_zone_is_refused" {
  command = plan
  variables {
    capacity_by_az = {
      "us-east-1b" = { cidr_block = "10.0.80.0/20", route_table_id = "rtb-00000000000000001" }
    }
  }
  expect_failures = [aws_subnet.capacity]
}
