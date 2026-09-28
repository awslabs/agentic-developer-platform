mock_provider "aws" {}
mock_provider "random" {}

variables {
  owner_email        = "test@example.invalid"
  state_bucket       = "test-state-bucket"
  vpc_id             = "vpc-0123456789abcdef0"
  private_subnet_ids = ["subnet-0123456789abcdef0"]
}

override_resource {
  target = aws_security_group.svc
  values = { id = "sg-0123456789abcdef0" }
}

run "endpoint_access_only_from_task_sg_on_https" {
  command = apply
  plan_options {
    target = [aws_vpc_security_group_ingress_rule.endpoints]
  }
  variables {
    endpoint_security_group_ids = ["sg-0fedcba9876543210"]
  }
  assert {
    condition = (
      length(aws_vpc_security_group_ingress_rule.endpoints) == 1 &&
      aws_vpc_security_group_ingress_rule.endpoints["sg-0fedcba9876543210"].security_group_id == "sg-0fedcba9876543210" &&
      aws_vpc_security_group_ingress_rule.endpoints["sg-0fedcba9876543210"].referenced_security_group_id == "sg-0123456789abcdef0" &&
      aws_vpc_security_group_ingress_rule.endpoints["sg-0fedcba9876543210"].ip_protocol == "tcp" &&
      aws_vpc_security_group_ingress_rule.endpoints["sg-0fedcba9876543210"].from_port == 443 &&
      aws_vpc_security_group_ingress_rule.endpoints["sg-0fedcba9876543210"].to_port == 443 &&
      aws_vpc_security_group_ingress_rule.endpoints["sg-0fedcba9876543210"].cidr_ipv4 == null &&
      aws_vpc_security_group_ingress_rule.endpoints["sg-0fedcba9876543210"].cidr_ipv6 == null
    )
    error_message = "Endpoint access must be only task-SG HTTPS, without CIDR admission."
  }
}

run "no_endpoint_access_when_unconfigured" {
  command = plan
  plan_options {
    target = [aws_vpc_security_group_ingress_rule.endpoints]
  }
  assert {
    condition     = length(aws_vpc_security_group_ingress_rule.endpoints) == 0
    error_message = "Default empty endpoint list must create no ingress rules."
  }
}
