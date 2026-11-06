mock_provider "aws" {}
variables {
  account_id = "123456789012"
  aws_region = "us-east-1"
}
run "builder_can_connect_and_pull_only_qualified_images" {
  command = plan
  assert {
    condition = (
      length(aws_security_group.image_builder) == 0 &&
      aws_iam_instance_profile.image_builder.role == aws_iam_role.image_builder.name &&
      jsondecode(aws_iam_role.image_builder.assume_role_policy).Statement[0].Principal.Service == "ec2.amazonaws.com" &&
      toset(flatten([for statement in jsondecode(aws_iam_role_policy.image_builder.policy).Statement : statement.Action])) == toset([
        "ssm:UpdateInstanceInformation",
        "ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel",
        "ec2messages:AcknowledgeMessage", "ec2messages:DeleteMessage", "ec2messages:FailMessage", "ec2messages:GetEndpoint", "ec2messages:GetMessages", "ec2messages:SendReply",
        "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"
      ]) &&
      jsondecode(aws_iam_role_policy.image_builder.policy).Statement[2].Resource == [
        "arn:aws:ecr:us-east-1:123456789012:repository/adp-gitlab-runner-job",
        "arn:aws:ecr:us-east-1:123456789012:repository/adp-gitlab-runner-helper"
      ] &&
      jsondecode(aws_iam_role_policy.image_builder.policy).Statement[0].Condition.StringEquals["aws:RequestedRegion"] == "us-east-1"
    )
    error_message = "Temporary builder must have only regional SSM transport and own job/helper image reads."
  }
}
