mock_provider "aws" {
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_ecr_repository" { defaults = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/adp-tools-facade" } }
  mock_resource "aws_kms_key" { defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001" } }
}

variables {
  aws_account_id                  = "123456789012"
  tools_facade_role_arn           = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
  gateway_arn                     = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
  enable_shared_tools_build       = true
  shared_tools_build_bucket       = "fictional-tools-state"
  shared_tools_lambda_repository  = "example/shared-lambda"
  shared_tools_browser_repository = "example/shared-browser"
}

run "shared_build_uses_only_selected_repositories" {
  command = apply
  assert {
    condition = (
      aws_codebuild_project.shared_build[0].name == "adp-dev-shared-tools-build" &&
      toset(jsondecode(aws_iam_role_policy.shared_build[0].policy).Statement[2].Resource) == toset([
        "arn:aws:ecr:us-east-1:123456789012:repository/example/shared-lambda",
        "arn:aws:ecr:us-east-1:123456789012:repository/example/shared-browser",
      ]) &&
      jsondecode(aws_iam_role.shared_build[0].assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == var.aws_account_id
    )
    error_message = "Only the selected repositories and pinned project may publish shared images."
  }
}

run "missing_private_repository_prevents_bootstrap" {
  command = plan
  variables { shared_tools_browser_repository = "" }
  expect_failures = [aws_codebuild_project.shared_build]
}

run "shared_build_is_disabled_without_operator_opt_in" {
  command = plan
  variables { enable_shared_tools_build = false }
  assert {
    condition     = length(aws_codebuild_project.shared_build) == 0
    error_message = "The bootstrap must be opt-in."
  }
}
