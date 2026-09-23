mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
}
variables {
  environment       = "test"
  name_prefix       = "adp-test"
  aws_region        = "eu-west-1"
  oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.eu-west-1.amazonaws.com/id/TEST"
  oidc_issuer       = "https://oidc.eks.eu-west-1.amazonaws.com/id/TEST"
}

run "service_escalation_is_explicitly_denied" {
  command = plan
  assert {
    condition = alltrue([for action in [
      "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration", "lambda:InvokeFunction",
      "codebuild:UpdateProject", "codebuild:CreateProject",
      "eks:CreateAccessEntry", "eks:AssociateAccessPolicy", "eks:AccessKubernetesApi",
      "kms:CreateGrant", "kms:PutKeyPolicy", "ssm:SendCommand", "ssm:GetParametersByPath",
      "s3:PutBucketPolicy", "ecr:PutImage",
      "dynamodb:PutItem", "secretsmanager:GetSecretValue", "iam:CreateRole", "iam:AttachRolePolicy", "sts:AssumeRole",
      "bedrock:PutModelInvocationLoggingConfiguration", "events:PutTargets"
    ] : !contains(one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOutsideRuntimeActions"]).NotAction, action)])
    error_message = "An untrusted runner can still act through an existing privileged service, promote an image, or access protected state."
  }
  assert {
    condition = toset(one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOtherOwnSmokeSourceResources"]).NotResource) == toset([
      "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-test-gateway-build-pr/*",
      "arn:aws:s3:::adp-test-security-scans-123456789012/security-agent/*"
    ])
    error_message = "Object access must remain restricted to smoke source and the existing scan ledger; never tenant objects or Terraform state."
  }
  assert {
    condition     = alltrue([for policy in [aws_iam_policy.runner_base.policy, aws_iam_policy.runner_services.policy, aws_iam_policy.runner_boundary.policy] : length(policy) <= 6144])
    error_message = "Rendered policies exceed IAM's 6144-character managed-policy quota."
  }
  assert {
    condition     = alltrue(flatten([for s in jsondecode(aws_iam_policy.runner_services.policy).Statement : [for action in s.Action : !can(regex("[*?]", action))]]))
    error_message = "Runner allows must enumerate concrete APIs, with no service wildcard."
  }
}

run "exact_legacy_transport_exception_preserves_engine_transport" {
  command = plan
  variables {
    transport_secret_arns = ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-key-Ab12Cd"]
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOtherLegacyEngineTransportResources"]).NotResource == ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-key-Ab12Cd"]
    error_message = "The transport exception permits more than its exact registered secret."
  }
  assert {
    condition     = length(aws_iam_policy.runner_boundary.policy) <= 6144
    error_message = "The boundary with a transport exception exceeds IAM quota."
  }
}

run "shared_gateway_project_requires_the_nonpublishing_role" {
  command = plan
  assert {
    condition = one([for s in jsondecode(aws_iam_policy.runner_services.policy).Statement : s if s.Sid == "StartSmokeBuild"]).Condition == {
      StringEquals = { "codebuild:serviceRole" = "arn:aws:iam::123456789012:role/adp-test-codebuild-gateway-pr" }
    }
    error_message = "PR StartBuild must require the exact nonpublishing service-role override."
  }
  assert {
    # Negated comparison also denies the absent condition key: omitting the
    # override cannot inherit the project's default publishing identity.
    condition = one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOtherBuildRole"]) == {
      Sid       = "DenyOtherBuildRole", Effect = "Deny", Action = ["codebuild:StartBuild"], Resource = "*",
      Condition = { StringNotEquals = { "codebuild:serviceRole" = "arn:aws:iam::123456789012:role/adp-test-codebuild-gateway-pr" } }
    }
    error_message = "An omitted or changed role must remain denied even if another policy grants StartBuild."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOtherStartSmokeBuildResources"]).NotResource == ["arn:aws:codebuild:eu-west-1:123456789012:project/adp-test-gateway-build"]
    error_message = "PR callers must not start other CodeBuild projects."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_policy.runner_services.policy).Statement : s if s.Sid == "PassSmokeRole"]).Resource == ["arn:aws:iam::123456789012:role/adp-test-codebuild-gateway-pr"] && one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOtherPassSmokeRoleResources"]).NotResource == ["arn:aws:iam::123456789012:role/adp-test-codebuild-gateway-pr"]
    error_message = "PassRole must allow only the PR identity and explicitly deny all other roles."
  }
  assert {
    condition = one([for s in jsondecode(aws_iam_policy.runner_services.policy).Statement : s if s.Sid == "PassSmokeRole"]).Condition == {
      StringEquals = { "iam:PassedToService" = "codebuild.amazonaws.com" }
      } && one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOtherPassService"]).Condition == {
      StringNotEquals = { "iam:PassedToService" = "codebuild.amazonaws.com" }
    }
    error_message = "The PR service role must not be passed to another AWS service."
  }
  assert {
    condition     = !contains(one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOutsideRuntimeActions"]).NotAction, "codebuild:RetryBuild") && !contains(one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOutsideRuntimeActions"]).NotAction, "codebuild:StartBuildBatch")
    error_message = "Retry/batch APIs must not bypass the StartBuild role condition."
  }
}
