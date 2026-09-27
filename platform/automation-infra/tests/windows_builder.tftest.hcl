mock_provider "external" {}
mock_provider "aws" {
  mock_data "aws_ami" { defaults = { owner_id = "099720109477" } }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/test-policy" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/test-role" }
  }
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}
override_data {
  target = data.aws_ssm_parameter.frontend_bucket
  values = { value = "adp-test-frontend" }
}
override_data {
  target = data.aws_ssm_parameter.frontend_cloudfront_id
  values = { value = "E1234567890" }
}
variables {
  build_project_names        = ["adp-test-superplane-executor"]
  name_prefix                = "adp-test"
  environment                = "test"
  aws_region                 = "us-east-1"
  cluster_name               = "adp-test-eks-cluster"
  repository                 = "aws-e/adp"
  windows_builder_ami_id     = "ami-0123456789abcdef0"
  enable_windows_builder     = true
  windows_builder_vpc_id     = "vpc-0123456789abcdef0"
  windows_builder_subnet_ids = ["subnet-0123456789abcdef0"]
  windows_cape_host_id       = "i-0123456789abcdef0"
}
run "bounded_windows_identity" {
  command = apply
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.windows_builder[0].policy).Statement : s if try(s.Sid, "") == "CanonicalImage"]).Resource == "arn:aws:ec2:us-east-1::image/ami-0123456789abcdef0"
    error_message = "Only the exact reviewed AMI may launch, never all Amazon images."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.windows_builder[0].policy).Statement : s if try(s.Sid, "") == "CreateBoundedBuilderRole"]).Resource == "arn:aws:iam::123456789012:role/adp-test-imgbuilder-ci-builder-role"
    error_message = "CI lifecycle must not adopt or delete legacy standalone builder roles."
  }
  assert {
    condition     = can(regex("^adp-[a-z0-9-]+-trusted-deployment$", aws_iam_role.windows_builder[0].name))
    error_message = "The role name must satisfy the shared trusted-deployment admission guard."
  }
  assert {
    condition     = jsondecode(aws_iam_role.windows_builder[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:aws-e/adp:environment:adp-windows-build-test"
    error_message = "Windows authority must have its own environment identity."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.windows_builder[0].policy).Statement : s if try(s.Sid, "") == "CreateBoundedBuilderRole"]).Condition.StringEquals["iam:PermissionsBoundary"] == aws_iam_policy.windows_builder_boundary[0].arn
    error_message = "An unbounded workload role must never be creatable."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.windows_builder[0].policy).Statement : s if try(s.Sid, "") == "PrivateSubnets"]).Resource == ["arn:aws:ec2:us-east-1:123456789012:subnet/subnet-0123456789abcdef0"]
    error_message = "Windows instances must be confined to inventoried private subnets."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.windows_builder[0].policy).Statement : s if try(s.Sid, "") == "PrivateLaunchNetwork"]).Condition.Bool["ec2:AssociatePublicIpAddress"] == "false" && one([for s in jsondecode(aws_iam_role_policy.windows_builder[0].policy).Statement : s if try(s.Sid, "") == "EncryptedLaunchStorage"]).Condition.Bool["ec2:Encrypted"] == "true"
    error_message = "The builder must use private interfaces and encrypted storage."
  }
  assert {
    condition     = !strcontains(aws_iam_role_policy.windows_builder[0].policy, "AWS-RunShellScript") && strcontains(aws_iam_role_policy.windows_builder[0].policy, "document/adp-test-register-cape-windows")
    error_message = "CAPE may only receive the immutable registration recipe, not arbitrary shell commands."
  }
  assert {
    condition     = jsondecode(aws_ssm_document.windows_registration[0].content).parameters.buildDate.allowedPattern == "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
    error_message = "Registration input must not permit shell or path injection."
  }
  assert {
    condition     = !strcontains(aws_iam_policy.windows_builder_boundary[0].policy, "secretsmanager:") && !strcontains(aws_iam_policy.windows_builder_boundary[0].policy, "sts:AssumeRole")
    error_message = "Workload ceiling must not expose tenant credentials or other identities."
  }
}
