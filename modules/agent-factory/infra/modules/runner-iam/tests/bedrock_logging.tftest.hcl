mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}

variables {
  environment       = "test"
  name_prefix       = "adp-test"
  aws_region        = "eu-west-1"
  oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/TEST"
  oidc_issuer       = "https://oidc.eks.eu-west-1.amazonaws.com/id/TEST"
}

run "dedicated_runner_name_avoids_legacy_codebuild_role" {
  command = plan
  variables {
    runner_role_name = "adp-test-agent-factory-runner-role"
  }
  assert {
    condition     = aws_iam_role.runner.name == "adp-test-agent-factory-runner-role"
    error_message = "The factory must use the selected IRSA role name."
  }
}

run "runner_can_manage_logging_within_its_boundary" {
  command = plan

  assert {
    condition = (
      length(aws_iam_policy.runner_services.policy) <= 6144 &&
      length(aws_iam_policy.runner_boundary.policy) <= 6144 &&
      length(aws_iam_role_policy.bedrock_invocation_logging.policy) < 5000
    )
    error_message = "Managed IAM policies must stay within the 6144-character limit."
  }

  assert {
    condition = alltrue([
      for action in [
        "bedrock:GetModelInvocationLoggingConfiguration",
        "bedrock:PutModelInvocationLoggingConfiguration",
        "bedrock:DeleteModelInvocationLoggingConfiguration",
      ] : contains(jsondecode(aws_iam_policy.runner_boundary.policy).Statement[0].Action, action)
    ])
    error_message = "The runner permissions boundary must permit Bedrock logging configuration management."
  }

  assert {
    condition = (
      toset(one([for s in jsondecode(aws_iam_role_policy.bedrock_invocation_logging.policy).Statement : s if s.Sid == "BedrockInvocationLogging"]).Action) == toset([
        "bedrock:GetModelInvocationLoggingConfiguration",
        "bedrock:PutModelInvocationLoggingConfiguration",
        "bedrock:DeleteModelInvocationLoggingConfiguration",
        "logs:DescribeLogGroups",
      ]) &&
      one([for s in jsondecode(aws_iam_role_policy.bedrock_invocation_logging.policy).Statement : s if s.Sid == "BedrockInvocationLogging"]).Condition.StringEquals["aws:RequestedRegion"] == "eu-west-1"
    )
    error_message = "Grant only the required control-plane actions in the runner's configured region."
  }

  assert {
    condition = toset(one([
      for s in jsondecode(aws_iam_role_policy.bedrock_invocation_logging.policy).Statement : s if s.Sid == "BedrockInvocationLogGroups"
      ]).Resource) == toset([
      "arn:aws:logs:eu-west-1:123456789012:log-group:/aws/bedrock/adp-test/model-invocations",
      "arn:aws:logs:eu-west-1:123456789012:log-group:/aws/bedrock/adp-test/model-invocations:*",
    ])
    error_message = "CloudWatch management must be scoped to this deployment's Bedrock log group."
  }

  assert {
    condition = alltrue([
      for action in ["s3:GetBucketOwnershipControls", "s3:PutBucketOwnershipControls", "s3:DeleteBucketOwnershipControls"] :
      contains(one([for s in jsondecode(aws_iam_role_policy.bedrock_invocation_logging.policy).Statement : s if s.Sid == "BedrockLogBucketOwnership"]).Action, action)
    ])
    error_message = "Terraform must be able to manage the ACL-disabled S3 logging destination."
  }
}
