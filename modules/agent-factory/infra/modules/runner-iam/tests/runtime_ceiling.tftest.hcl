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
      "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-test-gateway-build-pr/*"
    ])
    error_message = "Object access must remain restricted to smoke source only; never tenant objects or Terraform state."
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

run "transport_secret_decryption_is_bound_to_service_and_context" {
  command = plan
  variables {
    transport_secret_arns     = ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-key-Ab12Cd", "arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-id-Ef34Gh"]
    transport_secret_kms_arns = ["arn:aws:kms:eu-west-1:123456789012:key/11111111-2222-3333-4444-555555555555"]
  }
  # kms:Decrypt must be in the ceiling once KMS ARNs are supplied.
  assert {
    condition     = contains(one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "RuntimeApiCeiling"]).Action, "kms:Decrypt")
    error_message = "The ceiling must include kms:Decrypt when transport KMS ARNs are configured."
  }
  # The grant must require Secrets Manager ViaService and the exact secret context.
  assert {
    condition = one([for s in jsondecode(aws_iam_policy.runner_services.policy).Statement : s if s.Sid == "SecretDecryption"]).Condition.StringEquals == {
      "kms:ViaService"                  = "secretsmanager.eu-west-1.amazonaws.com"
      "kms:EncryptionContext:SecretARN" = ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-key-Ab12Cd", "arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-id-Ef34Gh"]
    }
    error_message = "Decrypt must require Secrets Manager and the exact transport secret encryption context."
  }
  # Boundary denies prevent another attached policy from bypassing these conditions.
  assert {
    condition = length([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s
    if contains(["DenyDirectKeyDecryption", "DenyOtherSecretDecryption"], s.Sid)]) == 2
    error_message = "The boundary must deny missing/wrong ViaService and encryption context."
  }
  assert {
    condition = one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyDirectKeyDecryption"]).Condition == {
      StringNotEquals = { "kms:ViaService" = "secretsmanager.eu-west-1.amazonaws.com" }
    }
    error_message = "DenyDirectKeyDecryption must refuse any decrypt not via Secrets Manager."
  }
  assert {
    condition = one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "DenyOtherSecretDecryption"]).Condition == {
      StringNotEquals = { "kms:EncryptionContext:SecretARN" = ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-key-Ab12Cd", "arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-id-Ef34Gh"] }
    }
    error_message = "DenyOtherSecretDecryption must refuse decrypt for any non-transport secret."
  }
  # All policies must still fit IAM's managed-policy limit with the KMS additions.
  # Compare complete deny statements: Sid presence alone permits an Allow,
  # wrong action/resource or reversed condition to silently weaken the ceiling.
  assert {
    condition = alltrue([for sid, conditions in {
      DenyDirectKeyDecryption   = { StringNotEquals = { "kms:ViaService" = "secretsmanager.eu-west-1.amazonaws.com" } }
      DenyOtherSecretDecryption = { StringNotEquals = { "kms:EncryptionContext:SecretARN" = ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-key-Ab12Cd", "arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-id-Ef34Gh"] } }
      } : jsonencode(one([for statement in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : statement if statement.Sid == sid])) == jsonencode({
        Sid = sid, Effect = "Deny", Action = ["kms:Decrypt"], Resource = "*", Condition = conditions
    })])
    error_message = "KMS boundary statements must deny decrypt globally with the exact negative service/context conditions."
  }
  assert {
    condition = jsonencode(one([for statement in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : statement if statement.Sid == "DenyOtherSecretDecryptionResources"])) == jsonencode({
      Sid         = "DenyOtherSecretDecryptionResources", Effect = "Deny", Action = ["kms:Decrypt"],
      NotResource = ["arn:aws:kms:eu-west-1:123456789012:key/11111111-2222-3333-4444-555555555555"]
    })
    error_message = "Other attached/resource policies must not grant decrypt on any unregistered key."
  }
  assert {
    condition = jsonencode(one([for statement in jsondecode(aws_iam_policy.runner_services.policy).Statement : statement if statement.Sid == "SecretDecryption"])) == jsonencode({
      Sid      = "SecretDecryption", Effect = "Allow", Action = ["kms:Decrypt"],
      Resource = ["arn:aws:kms:eu-west-1:123456789012:key/11111111-2222-3333-4444-555555555555"],
      Condition = { StringEquals = {
        "kms:ViaService"                  = "secretsmanager.eu-west-1.amazonaws.com",
        "kms:EncryptionContext:SecretARN" = ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-key-Ab12Cd", "arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-id-Ef34Gh"]
      } }
    })
    error_message = "Decrypt allow must include only the exact key, API, Secrets Manager service and approved secret context."
  }
  assert {
    condition = alltrue([for policy in [
      aws_iam_policy.runner_base.policy,
      aws_iam_policy.runner_services.policy,
      aws_iam_policy.runner_boundary.policy,
    ] : length(policy) <= 6144])
    error_message = "A policy with KMS grants exceeds IAM's 6144-character managed-policy quota."
  }
}

run "no_kms_grant_without_kms_arns" {
  command = plan
  variables {
    transport_secret_arns = ["arn:aws:secretsmanager:eu-west-1:123456789012:secret:adp/aws-e/gh-app-dev-key-Ab12Cd"]
  }
  assert {
    condition     = !contains(one([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if s.Sid == "RuntimeApiCeiling"]).Action, "kms:Decrypt")
    error_message = "Transport secrets without KMS ARNs must not add kms:Decrypt to the ceiling."
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_policy.runner_boundary.policy).Statement : s if try(s.Sid == "DenyDirectKeyDecryption", false)]) == 0
    error_message = "KMS boundary denies must not appear when KMS ARNs are not configured."
  }
}

run "active_runner_uses_environment_resources_and_exact_gateway_routes" {
  command = plan
  variables {
    name_prefix            = "adp-test-agent"
    gateway_execution_arns = ["arn:aws:execute-api:eu-west-1:123456789012:api123/test/POST/agent/*"]
  }
  assert {
    condition     = one([for s in module.runtime_policy.grants : s if s.Sid == "GatewayEndpoint"]).Resource == ["arn:aws:ssm:eu-west-1:123456789012:parameter/adp/test/gateway/apigw-invoke-url"]
    error_message = "The active runner suffix must not alter the deployed gateway parameter."
  }
  assert {
    condition     = one([for s in module.runtime_policy.grants : s if s.Sid == "StartSmokeBuild"]).Resource == ["arn:aws:codebuild:eu-west-1:123456789012:project/adp-test-gateway-build"]
    error_message = "The active runner must use the existing shared gateway project."
  }
  assert {
    condition     = toset(one([for s in module.runtime_policy.boundary : s if s.Sid == "DenyOtherGatewayTransportResources"]).NotResource) == toset(["arn:aws:execute-api:eu-west-1:123456789012:api123/test/POST/agent/*"])
    error_message = "Other API IDs, environments, methods and routes must remain explicitly denied."
  }
}
