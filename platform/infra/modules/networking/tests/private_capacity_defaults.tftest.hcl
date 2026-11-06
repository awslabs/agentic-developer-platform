mock_provider "aws" {}
override_data {
  target = data.aws_availability_zones.available
  values = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
}
variables {
  environment = "test"
  aws_region  = "us-east-1"
  name_prefix = "adp-test"
}
run "fresh_default_has_larger_private_subnets" {
  command = plan
  assert {
    condition     = [for subnet in aws_subnet.private : subnet.cidr_block] == ["10.0.64.0/20", "10.0.80.0/20"]
    error_message = "A new default deployment must have 4091 usable private addresses per AZ."
  }
  assert {
    condition     = [for subnet in aws_subnet.public : subnet.cidr_block] == ["10.0.1.0/24", "10.0.2.0/24"]
    error_message = "Larger private capacity must leave public subnet ranges unchanged and disjoint."
  }
}
run "legacy_installation_has_small_private_subnets" {
  command = apply
  variables { private_subnet_cidrs = ["10.0.10.0/24", "10.0.11.0/24"] }
}
run "upgrading_legacy_installation_does_not_resize_existing_subnets" {
  command = plan
  assert {
    condition     = [for subnet in aws_subnet.private : subnet.cidr_block] == ["10.0.10.0/24", "10.0.11.0/24"]
    error_message = "Updated defaults must not replace or resize existing private subnets."
  }
}
