mock_provider "external" {
  mock_data "external" { defaults = { result = { verified = "true" } } }
}
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" { defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" } }
  mock_data "aws_kms_key" { defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000" } }
  mock_data "aws_ssm_parameter" { defaults = { value = "mock" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
  mock_data "aws_subnet" { defaults = { vpc_id = "vpc-00000000000000001", map_public_ip_on_launch = false, availability_zone_id = "use1-az1" } }
  mock_data "aws_security_group" { defaults = { vpc_id = "vpc-00000000000000001" } }
}

variables {
  name_prefix               = "adp-dev"
  environment               = "dev"
  aws_region                = "us-east-1"
  cluster_name              = "adp-dev-eks-cluster"
  repository                = "aws-e/adp"
  build_project_names       = ["adp-dev-gateway-build"]
  enable_tools_deployment   = true
  tools_gateway_arn         = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
  tools_gateway_target_arns = ["arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example/target/existing"]
}

override_data {
  target = data.aws_ssm_parameter.frontend_bucket
  values = { value = "adp-dev-frontend" }
}
override_data {
  target = data.aws_ssm_parameter.frontend_cloudfront_id
  values = { value = "E1234567890" }
}
run "gateway_key_use_is_opt_in" {
  command = plan
  assert {
    condition = (
      length(aws_iam_policy.tools_gateway_key) == 0 &&
      length(aws_iam_role_policy_attachment.tools_gateway_key) == 0 &&
      !contains(local.tools_actions, "kms:GenerateDataKey") && !contains(local.tools_actions, "kms:ListGrants") &&
      one([for s in local.tools_environment_denials : s if s.Sid == "DenyOtherEnvironmentKeys"]).NotResource == [local.tools_lambda_environment_key]
    )
    error_message = "Gateway key access must default off; ordinary releases keep Lambda-only decryption."
  }
}

run "gateway_key_use_is_exact_and_preserves_lambda_isolation" {
  command = plan
  variables {
    tools_gateway_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
  }
  assert {
    condition = (
      one([for s in jsondecode(aws_iam_policy.tools_gateway_key[0].policy).Statement : s if s.Sid == "GatewayKeyValidation"]) == {
        Sid = "GatewayKeyValidation", Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = var.tools_gateway_kms_key_arn,
        Condition = {
          StringEquals = { "kms:ViaService" = "bedrock-agentcore.us-east-1.amazonaws.com", "kms:CallerAccount" = "123456789012" }
          Null         = { "kms:EncryptionContextKeys" = "true" }
        }
      } &&
      one([for s in jsondecode(aws_iam_policy.tools_gateway_key[0].policy).Statement : s if s.Sid == "GatewayGrantRead"]) == {
        Sid       = "GatewayGrantRead", Effect = "Allow", Action = ["kms:ListGrants"], Resource = var.tools_gateway_kms_key_arn,
        Condition = { StringEquals = { "kms:ViaService" = "bedrock-agentcore.us-east-1.amazonaws.com", "kms:CallerAccount" = "123456789012" } }
      } &&
      length([for s in jsondecode(aws_iam_policy.tools_gateway_key[0].policy).Statement : s if s.Effect == "Allow"]) == 2 &&
      alltrue([for policy in [aws_iam_role_policy.tools_deployment[0].policy, aws_iam_role_policy.tools_deployment_ceiling[0].policy] :
        alltrue([for action in ["kms:Decrypt", "kms:GenerateDataKey", "kms:ListGrants"] : contains(one([for s in jsondecode(policy).Statement : s if s.Sid == "DenyOtherAPIs"]).NotAction, action)])
      ]) &&
      alltrue([for action in ["kms:Encrypt", "kms:DescribeKey", "kms:Sign", "kms:CreateGrant", "kms:PutKeyPolicy"] : !contains(local.tools_actions, action)]) &&
      length(aws_iam_role_policy.tools_deployment[0].policy) + length(aws_iam_role_policy.tools_deployment_ceiling[0].policy) <= 10240 &&
      length(aws_iam_policy.tools_gateway_key[0].policy) <= 6144
    )
    error_message = "Only bound AgentCore data-key use is allowed; administration and grants remain denied within policy quotas."
  }
  assert {
    # Evaluate each negative dimension against the rendered explicit denials,
    # including an independent key-policy Allow and missing context.
    condition = alltrue([for input in [
      { action = "kms:GenerateDataKey", key = var.tools_gateway_kms_key_arn, service = local.tools_gateway_key_service, gateway = "", account = local.tools_account, function = "", deny = false },
      { action = "kms:Decrypt", key = var.tools_gateway_kms_key_arn, service = local.tools_gateway_key_service, gateway = var.tools_gateway_arn, account = local.tools_account, function = "", deny = true },
      { action = "kms:GenerateDataKey", key = var.tools_gateway_kms_key_arn, service = "", gateway = "", account = local.tools_account, function = "", deny = true },
      { action = "kms:Decrypt", key = var.tools_gateway_kms_key_arn, service = "lambda.us-east-1.amazonaws.com", gateway = var.tools_gateway_arn, account = local.tools_account, function = local.tools_function, deny = true },
      { action = "kms:GenerateDataKey", key = var.tools_gateway_kms_key_arn, service = "bedrock-agentcore.us-west-2.amazonaws.com", gateway = "", account = local.tools_account, function = "", deny = true },
      { action = "kms:Decrypt", key = var.tools_gateway_kms_key_arn, service = local.tools_gateway_key_service, gateway = "", account = local.tools_account, function = "", deny = false },
      { action = "kms:GenerateDataKey", key = var.tools_gateway_kms_key_arn, service = local.tools_gateway_key_service, gateway = "${var.tools_gateway_arn}-other", account = local.tools_account, function = "", deny = true },
      { action = "kms:Decrypt", key = var.tools_gateway_kms_key_arn, service = local.tools_gateway_key_service, gateway = var.tools_gateway_arn, account = "000000000000", function = "", deny = true },
      { action = "kms:Decrypt", key = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000099", service = local.tools_gateway_key_service, gateway = var.tools_gateway_arn, account = local.tools_account, function = "", deny = true },
      { action = "kms:GenerateDataKey", key = local.tools_lambda_environment_key, service = local.tools_gateway_key_service, gateway = var.tools_gateway_arn, account = local.tools_account, function = "", deny = true },
      { action = "kms:Decrypt", key = local.tools_lambda_environment_key, service = "lambda.us-east-1.amazonaws.com", gateway = "", account = local.tools_account, function = local.tools_function, deny = false },
      { action = "kms:Decrypt", key = local.tools_lambda_environment_key, service = "lambda.us-east-1.amazonaws.com", gateway = "", account = local.tools_account, function = local.tools_shared_function, deny = false },
      { action = "kms:Decrypt", key = local.tools_lambda_environment_key, service = local.tools_gateway_key_service, gateway = var.tools_gateway_arn, account = local.tools_account, function = local.tools_function, deny = true },
      { action = "kms:Decrypt", key = local.tools_lambda_environment_key, service = "lambda.us-east-1.amazonaws.com", gateway = "", account = local.tools_account, function = "unrelated-function", deny = true },
      { action = "kms:GenerateDataKey", key = var.tools_gateway_kms_key_arn, service = local.tools_gateway_key_service, gateway = "", account = "000000000000", function = "", deny = true },
      { action = "kms:GenerateDataKey", key = var.tools_gateway_kms_key_arn, service = local.tools_gateway_key_service, gateway = var.tools_gateway_arn, account = local.tools_account, function = "", deny = true },
      { action = "kms:GenerateDataKey", key = var.tools_gateway_kms_key_arn, service = local.tools_gateway_key_service, gateway = "", account = local.tools_account, function = "arbitrary-context", deny = true },
      { action = "kms:ListGrants", key = var.tools_gateway_kms_key_arn, service = local.tools_gateway_key_service, gateway = "", account = local.tools_account, function = "", deny = false },
      { action = "kms:ListGrants", key = var.tools_gateway_kms_key_arn, service = "", gateway = "", account = local.tools_account, function = "", deny = true },
      { action = "kms:ListGrants", key = var.tools_gateway_kms_key_arn, service = "bedrock-agentcore.us-west-2.amazonaws.com", gateway = "", account = local.tools_account, function = "", deny = true },
      { action = "kms:ListGrants", key = var.tools_gateway_kms_key_arn, service = local.tools_gateway_key_service, gateway = "", account = "000000000000", function = "", deny = true },
      { action = "kms:ListGrants", key = local.tools_lambda_environment_key, service = local.tools_gateway_key_service, gateway = "", account = local.tools_account, function = "", deny = true },
      ] : anytrue([for s in concat(jsondecode(aws_iam_policy.tools_gateway_key[0].policy).Statement, local.tools_environment_denials) : (
        s.Effect == "Deny" && contains(flatten([s.Action]), input.action) &&
        try(!contains(flatten([s.NotResource]), input.key), s.Resource == "*" || s.Resource == input.key) &&
        try(alltrue([for key, expected in s.Condition.StringNotEquals :
          !contains(flatten([expected]), lookup({
            "kms:ViaService"                                          = input.service, "kms:CallerAccount" = input.account,
            "kms:EncryptionContext:aws:bedrock-agentcore-gateway:arn" = input.gateway,
            "kms:EncryptionContext:aws:lambda:FunctionArn"            = input.function
          }, key, ""))
        ]), true) &&
        try(alltrue([for key, expected in s.Condition.Null :
          key == "kms:EncryptionContextKeys" && tostring(input.gateway == "" && input.function == "") == expected
        ]), true)
      )]) == input.deny
    ])
    error_message = "Explicit denies must reject other keys, direct calls, wrong service/region/gateway/account and foreign Lambda contexts."
  }
}

run "reject_foreign_key_account" {
  command = plan
  variables { tools_gateway_kms_key_arn = "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000001" }
  expect_failures = [var.tools_gateway_kms_key_arn]
}
run "reject_foreign_key_region" {
  command = plan
  variables { tools_gateway_kms_key_arn = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000001" }
  expect_failures = [var.tools_gateway_kms_key_arn]
}
run "reject_key_alias" {
  command = plan
  variables { tools_gateway_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:alias/gateway" }
  expect_failures = [var.tools_gateway_kms_key_arn]
}
run "reject_key_without_release_identity" {
  command = plan
  variables {
    enable_tools_deployment   = false
    tools_gateway_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
  }
  expect_failures = [var.tools_gateway_kms_key_arn]
}
run "reject_lambda_key_as_gateway_key" {
  command = plan
  variables { tools_gateway_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000" }
  expect_failures = [aws_iam_policy.tools_gateway_key]
}
