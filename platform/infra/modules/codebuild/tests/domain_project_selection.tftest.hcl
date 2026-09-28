mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
  mock_resource "aws_iam_policy" {
    defaults = { arn = "arn:aws:iam::123456789012:policy/mock-boundary" }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock-role" }
  }
}

variables {
  name_prefix                = "adp-test"
  state_bucket               = "adp-terraform-state-123456789012"
  account_id                 = "123456789012"
  aws_region                 = "us-east-1"
  ecr_registry               = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
  security_scans_bucket_arn  = "arn:aws:s3:::adp-security-scans-123456789012"
  security_scans_bucket_name = "adp-security-scans-123456789012"
}

run "base_platform_excludes_domain_builds" {
  command = plan
  assert {
    condition = (
      length([for name in keys(aws_codebuild_project.main) : name if startswith(name, "cyber-") || startswith(name, "superplane-")]) == 0 &&
      length([for name in keys(aws_iam_role.project) : name if startswith(name, "cyber-") || startswith(name, "superplane-")]) == 0
    )
    error_message = "A base platform must not create cyber or Superplane build projects or roles."
  }
}

run "domain_builds_require_explicit_app_selection" {
  command = plan
  variables {
    enabled_domain_apps = ["cyber", "superplane"]
  }
  assert {
    condition = (
      contains(keys(aws_codebuild_project.main), "cyber-browser") &&
      contains(keys(aws_codebuild_project.main), "cyber-worker") &&
      contains(keys(aws_codebuild_project.main), "superplane-executor")
    )
    error_message = "Selected domain apps must install their own declared build projects."
  }
}
