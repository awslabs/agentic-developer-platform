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

run "default_is_off" {
  command = plan
  assert {
    condition = (
      !contains(keys(aws_codebuild_project.main), "gitlab-runner-bootstrap") &&
      !contains(keys(aws_iam_role.project), "gitlab-runner-bootstrap") &&
      length(aws_ecr_repository.gitlab_runner_runtime) == 0
    )
    error_message = "The optional runner builder changed the default platform plan."
  }
}

run "scoped_builder_only_when_enabled" {
  command = apply
  variables {
    enable_gitlab_runner_build = true
  }
  assert {
    condition = (
      aws_codebuild_project.main["gitlab-runner-bootstrap"].source[0].buildspec == "codebuild/bs-gitlab-runner-bootstrap.yml" &&
      aws_iam_role.project["gitlab-runner-bootstrap"].name == "adp-test-codebuild-gitlab-runner-bootstrap" &&
      aws_codebuild_project.main["gitlab-runner-bootstrap"].environment[0].privileged_mode
    )
    error_message = "The approved bootstrap image build project or isolated role is missing."
  }
  assert {
    condition = alltrue([
      for statement in jsondecode(aws_iam_role_policy.project["gitlab-runner-bootstrap"].policy).Statement :
      !contains(flatten([try(statement.Action, [])]), "iam:PassRole") &&
      (statement.Sid != "OwnEcrRepositories" || statement.Resource == [
        "arn:aws:ecr:us-east-1:123456789012:repository/adp-gitlab-runner-bootstrap",
        "arn:aws:ecr:us-east-1:123456789012:repository/adp-gitlab-runner-helper",
        "arn:aws:ecr:us-east-1:123456789012:repository/adp-gitlab-runner-job"
      ]) &&
      (statement.Sid != "BuildSourceRead" || statement.Resource == "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-test-gitlab-runner-bootstrap/*") &&
      (statement.Sid != "RunnerHostArtifactRead" || statement.Resource == "arn:aws:s3:::adp-terraform-state-123456789012/gitlab-runners/upstream/*") &&
      (statement.Sid != "RunnerHostArtifactPublish" || statement.Resource == "arn:aws:s3:::adp-terraform-state-123456789012/gitlab-runners/artifacts/*") &&
      (statement.Sid != "RunnerHostAmiInspect" || statement.Condition.StringEquals["aws:RequestedRegion"] == "us-east-1")
    ])
    error_message = "The optional builder can access another project's images, source or host artifacts."
  }
  assert {
    condition = (
      toset(keys(aws_ecr_repository.gitlab_runner_runtime)) == toset(["bootstrap", "helper", "job"]) &&
      alltrue([for repo in aws_ecr_repository.gitlab_runner_runtime :
        repo.image_tag_mutability == "IMMUTABLE" && !repo.force_delete && repo.image_scanning_configuration[0].scan_on_push
      ])
    )
    error_message = "Runtime publication requires exactly three immutable retained repositories."
  }
}
