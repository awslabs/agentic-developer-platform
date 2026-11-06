mock_provider "aws" {
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}

variables {
  account_id  = "123456789012"
  aws_region  = "us-west-2"
  environment = "test"
  repository  = "example/platform"
}

run "no_implicit_activation_access" {
  command = plan
  assert {
    condition     = length(aws_iam_policy.runner_activation) == 0 && length(aws_iam_policy.runner_evidence) == 0 && length(aws_iam_role_policy.registration_tag_read) == 0
    error_message = "Identity setup must not implicitly add runner access."
  }
}

run "exact_state_and_read_only_foundation" {
  command = plan
  variables {
    runner_activation = {
      group_id              = 37
      cluster_name          = "adp-test-eks-cluster"
      access_entry_arn      = "arn:aws:eks:us-west-2:123456789012:access-entry/adp-test-eks-cluster/role/123456789012/adp-test-gitlab-trusted-deployment/11111111-2222-3333-4444-555555555555"
      eks_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-west-2.amazonaws.com/id/ABC123"
      vpc_id                = "vpc-0123456789abcdef0"
    }
    registration_tag_read = true
  }
  assert {
    condition = alltrue(flatten([for statement in jsondecode(aws_iam_policy.runner_activation[0].policy).Statement : [
      for action in flatten([statement.Action]) : can(regex("^[a-z0-9-]+:(Get|List|Describe)[A-Za-z]+$", action)) || (
        action == "s3:PutObject" && contains(["arn:aws:s3:::adp-terraform-state-123456789012/test/modules/gitlab-runners/terraform.tfstate", "arn:aws:s3:::adp-terraform-state-123456789012/test/modules/gitlab-runners/terraform.tfstate.tflock"], statement.Resource)
      ) || (action == "s3:DeleteObject" && statement.Resource == "arn:aws:s3:::adp-terraform-state-123456789012/test/modules/gitlab-runners/terraform.tfstate.tflock")
    ]]))
    error_message = "Only the runner state and its lock may be written; no cloud infrastructure, IAM, PassRole or token mutation."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.registration_tag_read[0].policy).Statement[0].Condition.StringEquals["aws:RequestedRegion"] == "us-west-2" && jsondecode(aws_iam_role_policy.registration_tag_read[0].policy).Statement[0].Action == "ec2:DescribeTags"
    error_message = "Post-update tag read must retain its exact region and action."
  }
}

run "reject_cross_account_access_entry" {
  command = plan
  variables {
    runner_activation = {
      group_id              = 37
      cluster_name          = "adp-test-eks-cluster"
      access_entry_arn      = "arn:aws:eks:us-west-2:999999999999:access-entry/adp-test-eks-cluster/role/999999999999/adp-test-gitlab-trusted-deployment/11111111-2222-3333-4444-555555555555"
      eks_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-west-2.amazonaws.com/id/ABC123"
      vpc_id                = "vpc-0123456789abcdef0"
    }
  }
  expect_failures = [var.runner_activation]
}

run "versioned_inputs_and_immutable_evidence" {
  command = plan
  variables {
    runner_evidence = {
      repository_id = 123
      input_versions = {
        "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-runners/inputs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/tfplan"            = "version-plan"
        "arn:aws:s3:::adp-test-deployment-evidence-123456789012/gitlab-runners/inputs/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/release-lock.json" = "version-lock"
      }
    }
  }
  assert {
    condition     = alltrue([for s in jsondecode(aws_iam_policy.runner_evidence[0].policy).Statement : s.Action == "s3:PutObject" ? s.Condition.StringEquals["s3:if-none-match"] == "*" : s.Action == "s3:GetObjectVersion"])
    error_message = "Every evidence write must be immutable; every input/evidence read must be versioned."
  }
}
