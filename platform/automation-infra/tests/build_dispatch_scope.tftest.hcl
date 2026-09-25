mock_provider "external" {}
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
}
variables {
  name_prefix         = "adp-test"
  environment         = "test"
  aws_region          = "us-east-1"
  cluster_name        = "adp-test-eks-cluster"
  repository          = "aws-e/adp"
  build_project_names = ["adp-test-superplane-executor"]
}

run "executor_has_exact_image_reads_and_no_worker_pointer_write" {
  command = plan
  variables {
    build_ecr_repository_names     = ["adp-superplane-executor"]
    build_publish_worker_image_tag = false
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "ReadPublishedImages"
    ]).Resource) == toset(["arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-executor"])
    error_message = "Executor dispatch may read only its explicitly selected repository."
  }
  assert {
    condition     = !can(regex("ssm:|PublishWorkerBuildTag", aws_iam_role_policy.build_dispatch.policy))
    error_message = "Executor-only dispatch must have no SSM publication capability."
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "DispatchKnownProjects"
      ]).Resource) == toset([
      "arn:aws:codebuild:us-east-1:123456789012:project/adp-test-superplane-executor",
      "arn:aws:codebuild:us-east-1:123456789012:build/adp-test-superplane-executor:*",
    ])
    error_message = "Repository scoping must not widen the reviewed build-project inventory."
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "StageReviewedSource"
    ]).Resource) == toset(["arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-test-superplane-executor/*"])
    error_message = "Only the selected project's source prefix may be staged."
  }
}

run "omitted_inputs_preserve_existing_dispatcher_capabilities" {
  command = plan
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "ReadPublishedImages"
    ]).Resource) == toset(["arn:aws:ecr:us-east-1:123456789012:repository/adp-*"])
    error_message = "Existing multi-image dispatchers retain their previous read scope until explicitly configured."
  }
  assert {
    condition = one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "PublishWorkerBuildTag"
    ]).Resource == "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/cyber/worker-image-tag"
    error_message = "Existing worker build consumers retain their exact publication pointer."
  }
}

run "multiple_explicit_repositories_remain_exact" {
  command = plan
  variables {
    build_ecr_repository_names = ["adp-superplane-executor", "adp-superplane-api"]
  }
  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.build_dispatch.policy).Statement : s if s.Sid == "ReadPublishedImages"
      ]).Resource) == toset([
      "arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-executor",
      "arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-api",
    ])
    error_message = "Explicit repository names must never collapse to a prefix grant."
  }
}

run "explicit_wildcard_is_refused" {
  command = plan
  variables { build_ecr_repository_names = ["adp-*"] }
  expect_failures = [var.build_ecr_repository_names]
}
run "explicit_empty_inventory_is_refused" {
  command = plan
  variables { build_ecr_repository_names = [] }
  expect_failures = [var.build_ecr_repository_names]
}
run "repository_arn_instead_of_name_is_refused" {
  command = plan
  variables { build_ecr_repository_names = ["arn:aws:ecr:us-east-1:123456789012:repository/adp-superplane-executor"] }
  expect_failures = [var.build_ecr_repository_names]
}
