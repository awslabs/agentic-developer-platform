# Exercise the app-only root in the existing no-AWS Terraform CI job.
mock_provider "aws" {}

variables {
  account_id  = "111122223333"
  region      = "us-west-2"
  environment = "fixture"
}

run "disabled_creates_nothing" {
  command = plan
  module { source = "../paid-worker-build" }
  assert {
    condition     = length(aws_codebuild_project.build) == 0 && length(aws_iam_role.build) == 0 && length(aws_iam_role_policy.build) == 0 && length(aws_iam_policy.boundary) == 0 && length(aws_cloudwatch_log_group.build) == 0
    error_message = "Omitted enablement must create no paid-worker build resources."
  }
}

run "enabled_is_dedicated_and_bounded" {
  command = apply
  module { source = "../paid-worker-build" }
  variables { enabled = true }
  override_resource {
    target = aws_iam_role.build["paid"]
    values = { arn = "arn:aws:iam::111122223333:role/adp-fixture-codebuild-superplane-paid-worker" }
  }
  override_resource {
    target = aws_iam_policy.boundary["paid"]
    values = { arn = "arn:aws:iam::111122223333:policy/adp-fixture-superplane-paid-build-boundary" }
  }
  assert {
    condition     = output.project_names == ["adp-fixture-superplane-paid-worker"] && aws_codebuild_project.build["paid"].build_timeout == 60 && aws_codebuild_project.build["paid"].queued_timeout == 480
    error_message = "Only the dedicated bounded paid project may be created."
  }
  assert {
    condition     = aws_codebuild_project.build["paid"].source[0].buildspec == "modules/domain-apps/superplane/releases/buildspecs/paid-worker.yml" && aws_codebuild_project.build["paid"].source[0].location == "adp-terraform-state-111122223333/codebuild/src/adp-fixture-superplane-paid-worker/explicit-source-required.zip"
    error_message = "Project must consume only its reviewed release source."
  }
  assert {
    condition     = aws_iam_role.build["paid"].permissions_boundary == aws_iam_policy.boundary["paid"].arn && aws_iam_role_policy.build["paid"].policy == aws_iam_policy.boundary["paid"].policy
    error_message = "The build role and its boundary must have the same narrow authority."
  }
  assert {
    condition     = toset(flatten([for s in jsondecode(local.build_policy).Statement : s.Action])) == toset(["logs:CreateLogStream", "logs:PutLogEvents", "s3:GetObject", "s3:GetObjectVersion", "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload", "ecr:DescribeImages", "ecr:DescribeRepositories", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload", "ecr:ListImages", "ecr:PutImage", "ecr:UploadLayerPart"])
    error_message = "Build identity must not acquire provisioning, role assumption, source-write or secret authority."
  }
  assert {
    condition     = alltrue([for s in jsondecode(local.build_policy).Statement : toset(s.Resource) == toset(s.Sid == "OwnEcrRepository" ? ["arn:aws:ecr:us-west-2:111122223333:repository/adp-superplane-paid-worker"] : s.Sid == "BuildSourceRead" ? ["arn:aws:s3:::adp-terraform-state-111122223333/codebuild/src/adp-fixture-superplane-paid-worker/*"] : s.Sid == "EcrAuth" ? ["*"] : ["arn:aws:logs:us-west-2:111122223333:log-group:/aws/codebuild/adp-fixture-superplane-paid-worker", "arn:aws:logs:us-west-2:111122223333:log-group:/aws/codebuild/adp-fixture-superplane-paid-worker:*"])])
    error_message = "Build resources must be restricted to the selected app/account/region."
  }
  assert {
    condition     = jsondecode(aws_iam_role.build["paid"].assume_role_policy).Statement[0].Condition.StringEquals == { "aws:SourceAccount" = "111122223333", "aws:SourceArn" = "arn:aws:codebuild:us-west-2:111122223333:project/adp-fixture-superplane-paid-worker" }
    error_message = "Only the exact CodeBuild project can assume its role."
  }
  assert {
    condition     = toset([for v in aws_codebuild_project.build["paid"].environment[0].environment_variable : v.name]) == toset(["ACCOUNT_ID", "REGISTRY"])
    error_message = "Paid builds must not depend on shared security-scanner configuration."
  }
}
