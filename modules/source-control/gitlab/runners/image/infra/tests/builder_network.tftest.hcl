mock_provider "aws" {
  mock_data "aws_vpc" {
    defaults = { owner_id = "123456789012" }
  }
  mock_data "aws_prefix_list" {
    defaults = { id = "pl-00000000" }
  }
}

variables {
  account_id = "123456789012"
  aws_region = "us-east-1"
  builder_network = {
    vpc_id = "vpc-00000000"
    interface_endpoint_ids = [
      "vpce-00000001", "vpce-00000002", "vpce-00000003", "vpce-00000004"
    ]
  }
}

override_data {
  target = data.aws_vpc_endpoint.builder["vpce-00000001"]
  values = {
    vpc_id                = "vpc-00000000"
    vpc_endpoint_type     = "Interface"
    state                 = "available"
    private_dns_enabled   = true
    service_name          = "com.amazonaws.us-east-1.ssm"
    network_interface_ids = ["eni-00000001"]
  }
}
override_data {
  target = data.aws_network_interface.builder_endpoint["eni-00000001"]
  values = {
    vpc_id     = "vpc-00000000"
    owner_id   = "123456789012"
    private_ip = "10.0.1.1"
  }
}

override_data {
  target = data.aws_vpc_endpoint.builder["vpce-00000002"]
  values = {
    vpc_id                = "vpc-00000000"
    vpc_endpoint_type     = "Interface"
    state                 = "available"
    private_dns_enabled   = true
    service_name          = "com.amazonaws.us-east-1.ssmmessages"
    network_interface_ids = ["eni-00000002"]
  }
}
override_data {
  target = data.aws_network_interface.builder_endpoint["eni-00000002"]
  values = {
    vpc_id     = "vpc-00000000"
    owner_id   = "123456789012"
    private_ip = "10.0.1.2"
  }
}

override_data {
  target = data.aws_vpc_endpoint.builder["vpce-00000003"]
  values = {
    vpc_id                = "vpc-00000000"
    vpc_endpoint_type     = "Interface"
    state                 = "available"
    private_dns_enabled   = true
    service_name          = "com.amazonaws.us-east-1.ecr.api"
    network_interface_ids = ["eni-00000003"]
  }
}
override_data {
  target = data.aws_network_interface.builder_endpoint["eni-00000003"]
  values = {
    vpc_id     = "vpc-00000000"
    owner_id   = "123456789012"
    private_ip = "10.0.1.3"
  }
}

override_data {
  target = data.aws_vpc_endpoint.builder["vpce-00000004"]
  values = {
    vpc_id                = "vpc-00000000"
    vpc_endpoint_type     = "Interface"
    state                 = "available"
    private_dns_enabled   = true
    service_name          = "com.amazonaws.us-east-1.ecr.dkr"
    network_interface_ids = ["eni-00000004"]
  }
}
override_data {
  target = data.aws_network_interface.builder_endpoint["eni-00000004"]
  values = {
    vpc_id     = "vpc-00000000"
    owner_id   = "123456789012"
    private_ip = "10.0.1.4"
  }
}

run "private_builder_reaches_only_endpoint_interfaces_and_s3" {
  command = plan
  assert {
    condition = (
      length(aws_security_group.image_builder) == 1 &&
      length(aws_security_group.image_builder[0].ingress) == 0 &&
      length(aws_security_group.image_builder[0].egress) == 2 &&
      alltrue([for rule in aws_security_group.image_builder[0].egress :
        rule.protocol == "tcp" && rule.from_port == 443 && rule.to_port == 443 &&
        length(coalesce(rule.ipv6_cidr_blocks, [])) == 0 && length(coalesce(rule.security_groups, [])) == 0
      ]) &&
      toset(flatten([for rule in aws_security_group.image_builder[0].egress : coalesce(rule.cidr_blocks, [])])) == toset([
        "10.0.1.1/32", "10.0.1.2/32", "10.0.1.3/32", "10.0.1.4/32"
      ]) &&
      toset(flatten([for rule in aws_security_group.image_builder[0].egress : coalesce(rule.prefix_list_ids, [])])) == toset(["pl-00000000"])
    )
    error_message = "The builder must reach HTTPS on exact SSM/ECR ENIs and regional S3 only, with no ingress or shared-SG destination."
  }
}

run "reject_missing_registry_endpoint" {
  command = plan
  override_data {
    target = data.aws_vpc_endpoint.builder["vpce-00000004"]
    values = {
      vpc_id                = "vpc-00000000"
      vpc_endpoint_type     = "Interface"
      state                 = "available"
      private_dns_enabled   = true
      service_name          = "com.amazonaws.us-east-1.ssm"
      network_interface_ids = ["eni-00000004"]
    }
  }
  expect_failures = [aws_security_group.image_builder]
}

run "reject_unrelated_service" {
  command = plan
  override_data {
    target = data.aws_vpc_endpoint.builder["vpce-00000004"]
    values = {
      vpc_id                = "vpc-00000000"
      vpc_endpoint_type     = "Interface"
      state                 = "available"
      private_dns_enabled   = true
      service_name          = "com.amazonaws.us-east-1.secretsmanager"
      network_interface_ids = ["eni-00000004"]
    }
  }
  expect_failures = [aws_security_group.image_builder]
}

run "reject_wrong_endpoint_vpc" {
  command = plan
  override_data {
    target = data.aws_vpc_endpoint.builder["vpce-00000004"]
    values = {
      vpc_id                = "vpc-00000099"
      vpc_endpoint_type     = "Interface"
      state                 = "available"
      private_dns_enabled   = true
      service_name          = "com.amazonaws.us-east-1.ecr.dkr"
      network_interface_ids = ["eni-00000004"]
    }
  }
  expect_failures = [aws_security_group.image_builder]
}

run "reject_public_endpoint_dns" {
  command = plan
  override_data {
    target = data.aws_vpc_endpoint.builder["vpce-00000004"]
    values = {
      vpc_id                = "vpc-00000000"
      vpc_endpoint_type     = "Interface"
      state                 = "available"
      private_dns_enabled   = false
      service_name          = "com.amazonaws.us-east-1.ecr.dkr"
      network_interface_ids = ["eni-00000004"]
    }
  }
  expect_failures = [aws_security_group.image_builder]
}

run "reject_unavailable_endpoint" {
  command = plan
  override_data {
    target = data.aws_vpc_endpoint.builder["vpce-00000004"]
    values = {
      vpc_id                = "vpc-00000000"
      vpc_endpoint_type     = "Interface"
      state                 = "pending"
      private_dns_enabled   = true
      service_name          = "com.amazonaws.us-east-1.ecr.dkr"
      network_interface_ids = ["eni-00000004"]
    }
  }
  expect_failures = [aws_security_group.image_builder]
}

run "reject_foreign_endpoint_interface" {
  command = plan
  override_data {
    target = data.aws_network_interface.builder_endpoint["eni-00000004"]
    values = {
      vpc_id     = "vpc-00000000"
      owner_id   = "000000000000"
      private_ip = "10.0.1.4"
    }
  }
  expect_failures = [aws_security_group.image_builder]
}
