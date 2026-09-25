mock_provider "external" {
  mock_data "external" { defaults = { result = { verified = "true" } } }
}
mock_provider "aws" {
  mock_data "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/adp-test-worker", permissions_boundary = "arn:aws:iam::123456789012:policy/adp-test-workload-ceiling" } }
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
}
variables {
  name_prefix         = "adp-test"
  environment         = "test"
  aws_region          = "us-east-1"
  cluster_name        = "adp-test-eks-cluster"
  repository          = "aws-e/adp"
  build_project_names = ["adp-test-gateway-build"]
  webhook_code_role_boundaries = {
    "arn:aws:iam::123456789012:role/adp-test-worker" = "arn:aws:iam::123456789012:policy/adp-test-workload-ceiling"
  }
}

run "webhook_code_profile_has_dedicated_oidc_trust" {
  command = plan
  variables {
    webhook_code_lambda_targets = {
      "arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook" = "arn:aws:iam::123456789012:role/adp-test-worker"
    }
    webhook_code_archive_prefix = "lambda-artifacts/webhook-ingress"
  }
  assert {
    condition = jsondecode(aws_iam_role.webhook_code[0].assume_role_policy).Statement[0].Condition.StringEquals == {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:aws-e/adp:environment:adp-webhook-code-test"
    }
    error_message = "Webhook-code must require its own dedicated protected environment, not the generic deployment environment."
  }
  assert {
    condition     = aws_iam_role.webhook_code[0].max_session_duration == 3600
    error_message = "Webhook-code sessions should be shorter than full deployment sessions."
  }
}

run "webhook_code_allows_only_lambda_read_and_update" {
  command = plan
  variables {
    webhook_code_lambda_targets = {
      "arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook" = "arn:aws:iam::123456789012:role/adp-test-worker"
    }
    webhook_code_archive_prefix = "lambda-artifacts/webhook-ingress"
  }
  assert {
    condition     = toset(one([for s in jsondecode(aws_iam_role_policy.webhook_code_lambda[0].policy).Statement : s if s.Sid == "ReadAndUpdateAdmittedFunctions"]).Action) == toset(["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode"])
    error_message = "Lambda policy must grant exactly read and code-update actions."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.webhook_code_lambda[0].policy).Statement : s if s.Sid == "ReadAndUpdateAdmittedFunctions"]).Resource == ["arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook"]
    error_message = "Lambda policy must scope to the exact admitted function."
  }
}

run "webhook_code_denies_iam_eks_and_role_chaining" {
  command = plan
  variables {
    webhook_code_lambda_targets = {
      "arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook" = "arn:aws:iam::123456789012:role/adp-test-worker"
    }
    webhook_code_archive_prefix = "lambda-artifacts/webhook-ingress"
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyAllIAM"]).Action == ["iam:*"]
    error_message = "Webhook-code must deny all IAM operations."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyAllEKS"]).Action == ["eks:*"]
    error_message = "Webhook-code must deny all EKS operations."
  }
  assert {
    condition     = toset(one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyRoleChaining"]).Action) == toset(["sts:AssumeRole", "sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity"])
    error_message = "Webhook-code must deny all role-chaining operations."
  }
}

run "webhook_code_denies_unlisted_lambda_mutations" {
  command = plan
  variables {
    webhook_code_lambda_targets = {
      "arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook" = "arn:aws:iam::123456789012:role/adp-test-worker"
    }
    webhook_code_archive_prefix = "lambda-artifacts/webhook-ingress"
  }
  assert {
    condition     = contains(one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyUnlistedLambdaMutations"]).Action, "lambda:CreateFunction")
    error_message = "Webhook-code must deny lambda:CreateFunction."
  }
  assert {
    condition     = contains(one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyUnlistedLambdaMutations"]).Action, "lambda:InvokeFunction")
    error_message = "Webhook-code must deny lambda:InvokeFunction."
  }
  assert {
    condition     = contains(one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyUnlistedLambdaMutations"]).Action, "lambda:UpdateFunctionConfiguration")
    error_message = "Webhook-code must deny lambda:UpdateFunctionConfiguration."
  }
}

run "webhook_code_denies_other_lambda_targets" {
  command = plan
  variables {
    webhook_code_lambda_targets = {
      "arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook" = "arn:aws:iam::123456789012:role/adp-test-worker"
    }
    webhook_code_archive_prefix = "lambda-artifacts/webhook-ingress"
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyOtherLambdaTargets"]).NotResource == ["arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook"]
    error_message = "Webhook-code must deny Lambda operations on any function not in the admitted set."
  }
}

run "webhook_code_scopes_s3_to_archive_prefix" {
  command = plan
  variables {
    webhook_code_lambda_targets = {
      "arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook" = "arn:aws:iam::123456789012:role/adp-test-worker"
    }
    webhook_code_archive_prefix = "lambda-artifacts/webhook-ingress"
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.webhook_code_lambda[0].policy).Statement : s if s.Sid == "ReadWriteCodeArchive"]).Resource == "arn:aws:s3:::adp-terraform-state-123456789012/lambda-artifacts/webhook-ingress/*"
    error_message = "S3 access must be scoped to the exact archive prefix."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyOtherS3Prefixes"]).NotResource == "arn:aws:s3:::adp-terraform-state-123456789012/lambda-artifacts/webhook-ingress/*"
    error_message = "S3 write must be denied outside the archive prefix."
  }
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyTrustedAutomationState"]).Action == ["s3:*"]
    error_message = "Webhook-code must deny all access to the trusted-automation state prefix."
  }
}

run "empty_targets_produce_no_webhook_code_role" {
  command = plan
  variables {
    webhook_code_lambda_targets = {}
    webhook_code_archive_prefix = ""
  }
  assert {
    condition     = length(aws_iam_role.webhook_code) == 0
    error_message = "Empty targets must not create a webhook-code role."
  }
  assert {
    condition     = length(aws_iam_role_policy.webhook_code_lambda) == 0
    error_message = "Empty targets must not create webhook-code policies."
  }
  assert {
    condition     = length(aws_iam_role_policy.webhook_code_ceiling) == 0
    error_message = "Empty targets must not create webhook-code ceiling."
  }
}

run "no_eks_access_entry_for_webhook_code" {
  command = apply
  variables {
    webhook_code_lambda_targets = {
      "arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook" = "arn:aws:iam::123456789012:role/adp-test-worker"
    }
    webhook_code_archive_prefix = "lambda-artifacts/webhook-ingress"
  }
  override_data {
    target = data.aws_iam_role.admitted_workload["arn:aws:iam::123456789012:role/adp-test-worker"]
    values = {
      arn                  = "arn:aws:iam::123456789012:role/adp-test-worker"
      permissions_boundary = "arn:aws:iam::123456789012:policy/adp-test-workload-ceiling"
    }
  }
  # The webhook-code role must not have any EKS access entries. The ceiling
  # denies eks:* and no access entry resource is defined for the webhook_code role.
  assert {
    condition     = one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyAllEKS"]).Resource == "*"
    error_message = "Webhook-code profile must deny all EKS access."
  }
}

run "generic_deployment_unchanged_with_webhook_code_enabled" {
  command = plan
  variables {
    webhook_code_lambda_targets = {
      "arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook" = "arn:aws:iam::123456789012:role/adp-test-worker"
    }
    webhook_code_archive_prefix = "lambda-artifacts/webhook-ingress"
  }
  # Verify the generic deployment role's OIDC trust is unchanged
  assert {
    condition     = jsondecode(aws_iam_role.deployment.assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:aws-e/adp:environment:adp-deploy-test"
    error_message = "The generic deployment role trust must be unchanged by the webhook-code profile."
  }
}

run "webhook_code_finite_ceiling_contains_resource_policy_grants" {
  command = plan
  variables {
    webhook_code_lambda_targets = {
      "arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook" = "arn:aws:iam::123456789012:role/adp-test-worker"
    }
    webhook_code_archive_prefix = "lambda-artifacts/webhook-ingress"
  }
  assert {
    condition = toset(one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyOutsideCodeDeployment"]).NotAction) == toset([
      "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode",
      "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:ListBucket", "sts:GetCallerIdentity"
    ])
    error_message = "Webhook deployment must explicitly deny every API outside its finite profile."
  }
  assert {
    condition     = contains(one([for s in jsondecode(aws_iam_role_policy.webhook_code_ceiling[0].policy).Statement : s if s.Sid == "DenyOtherS3Prefixes"]).Action, "s3:GetObjectVersion")
    error_message = "Versioned reads outside the archive must also be explicitly denied."
  }
}

run "webhook_code_requires_successful_live_admission" {
  command = plan
  variables {
    webhook_code_lambda_targets = {
      "arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook" = "arn:aws:iam::123456789012:role/adp-test-worker"
    }
    webhook_code_archive_prefix = "lambda-artifacts/webhook-ingress"
  }
  override_data {
    target = data.external.webhook_code_admission[0]
    values = { result = { verified = "false" } }
  }
  expect_failures = [aws_iam_role.webhook_code[0]]
}

run "webhook_only_inventory_never_admits_generic_cluster_admin" {
  command = plan
  variables {
    deployment_role_boundaries = {}
    webhook_code_lambda_targets = {
      "arn:aws:lambda:us-east-1:123456789012:function:adp-test-github-webhook" = "arn:aws:iam::123456789012:role/adp-test-worker"
    }
    webhook_code_archive_prefix = "lambda-artifacts/webhook-ingress"
  }
  assert {
    condition     = length(aws_eks_access_entry.deployment) == 0 && length(aws_eks_access_policy_association.deployment) == 0
    error_message = "Webhook-only admission must never enable generic EKS access."
  }
  assert {
    condition     = length(data.external.workload_admission) == 0
    error_message = "Webhook-only inventory must remain separate from generic cluster admission."
  }
}
