mock_provider "aws" {}
variables {
  account_id = "123456789012"
  aws_region = "us-east-1"
}
override_data {
  target = data.aws_iam_policy.existing_boundary
  values = { arn = "arn:aws:iam::123456789012:policy/adp-dev-codebuild-boundary" }
}
run "only_scoped_builder_and_immutable_repositories" {
  command = plan
  assert {
    condition = (
      length(aws_ecr_repository.runtime) == 3 &&
      alltrue([for repo in aws_ecr_repository.runtime : repo.image_tag_mutability == "IMMUTABLE" && !repo.force_delete]) &&
      aws_codebuild_project.builder.name == "adp-dev-gitlab-runner-bootstrap" &&
      aws_codebuild_project.builder.source[0].type == "S3" &&
      aws_codebuild_project.builder.source[0].buildspec == "codebuild/bs-gitlab-runner-bootstrap.yml" &&
      aws_codebuild_project.builder.environment[0].privileged_mode &&
      aws_iam_role.builder.permissions_boundary == data.aws_iam_policy.existing_boundary.arn &&
      jsondecode(aws_iam_role.builder.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012"
    )
    error_message = "The builder must use the existing permission boundary and only its immutable images."
  }
  assert {
    condition = (
      toset(flatten([for statement in jsondecode(aws_iam_role_policy.builder.policy).Statement : statement.Action])) == toset([
        "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject",
        "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
        "ecr:DescribeImages", "ecr:DescribeRepositories", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload",
        "ecr:ListImages", "ecr:PutImage", "ecr:UploadLayerPart", "ec2:DescribeImages", "sts:GetCallerIdentity"
      ]) &&
      jsondecode(aws_iam_role_policy.builder.policy).Statement[5].Resource == [
        "arn:aws:ecr:us-east-1:123456789012:repository/adp-gitlab-runner-bootstrap",
        "arn:aws:ecr:us-east-1:123456789012:repository/adp-gitlab-runner-helper",
        "arn:aws:ecr:us-east-1:123456789012:repository/adp-gitlab-runner-job"
      ]
    )
    error_message = "Builder permissions must remain scoped to its source, logs, receipts and images."
  }
}
