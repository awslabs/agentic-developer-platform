mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::111122223333:role/mock-build" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  environment          = "dev"
  aws_region           = "us-east-1"
  account_id           = "111122223333"
  cors_allowed_origins = ["https://superplane.dev.adp.internal"]
  database_secret_name = "adp/dev/superplane/database"
  jwt_secret_name      = "adp/dev/superplane/jwt-signing-key"
}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
  }
}

override_data {
  target = data.terraform_remote_state.platform
  values = {
    outputs = {
      eks_oidc_provider_arn          = "arn:aws:iam::111122223333:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
      eks_oidc_issuer                = "oidc.eks.us-east-1.amazonaws.com/id/EXAMPLED539D4633E53DE1B716D3041E"
      gateway_service_irsa_role_name = "adp-dev-role-gateway-service"
      codebuild_boundary_arn         = "arn:aws:iam::111122223333:policy/adp-dev-codebuild-boundary"
      security_scans_bucket_name     = "adp-dev-security-scans"
    }
  }
}

run "observer_omitted_by_default" {
  command = plan
  assert {
    condition     = length(aws_iam_role.lifecycle_observer) == 0
    error_message = "Recovery reads require explicit installation selection."
  }
}
run "observer_scoped_api_and_regions" {
  command = plan
  variables {
    api_producer_role             = { api_id = "abcdefghij", stage = "dev" }
    lifecycle_observation_regions = ["us-west-2"]
  }
  assert {
    condition     = jsondecode(aws_iam_role.lifecycle_observer[0].assume_role_policy).Statement[0].Principal.AWS == "arn:aws:iam::111122223333:role/adp-dev-superplane-api-producer"
    error_message = "Only the exact API role may assume observation authority."
  }
  assert {
    condition     = jsondecode(one(aws_iam_role.lifecycle_observer[0].inline_policy).policy).Statement[0].Condition.StringEquals["aws:RequestedRegion"] == ["us-west-2"]
    error_message = "Regional reads must stay within the selected region."
  }
  assert {
    condition     = alltrue([for statement in jsondecode(one(aws_iam_role.lifecycle_observer[0].inline_policy).policy).Statement : alltrue([for action in flatten([statement.Action]) : can(regex(":(Get|List|Describe)[A-Za-z]+$", action))])])
    error_message = "Observer cannot gain mutation actions or wildcards."
  }
}
