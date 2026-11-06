mock_provider "external" {
  mock_data "external" { defaults = { result = { verified = "true" } } }
}
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_data "aws_ssm_parameter" { defaults = { value = "mock" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
}

variables {
  name_prefix                     = "adp-test"
  environment                     = "test"
  aws_region                      = "us-east-1"
  cluster_name                    = "adp-test-eks-cluster"
  repository                      = "example/tools"
  build_project_names             = ["adp-test-gateway-build"]
  enable_shared_tools_deployment  = true
  shared_tools_state_bucket       = "fictional-tools-state"
  shared_tools_state_key          = "existing/tools/agentcore/release.tfstate"
  shared_tools_resource_name      = "fictional-tools"
  shared_tools_lambda_repository  = "fictional/shared-lambda"
  shared_tools_browser_repository = "fictional/shared-browser"
}

run "release_uses_dedicated_subject_state_and_browser_group" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "fictional-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E12345678" }
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.shared_tools_deployment[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:example/tools:environment:adp-shared-tools-deploy-test" &&
      toset(aws_eks_access_entry.shared_tools_deployment[0].kubernetes_groups) == toset(["adp:shared-tools-release-test"])
    )
    error_message = "The provider release may assume only its own protected identity and namespace group."
  }
  assert {
    condition = (
      length([for statement in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement : statement if can(index(statement.Resource, "arn:aws:s3:::fictional-tools-state/existing/tools/agentcore/release.tfstate"))]) == 1 &&
      length([for statement in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement : statement if statement.Resource == "arn:aws:s3:::fictional-tools-state/tools/platform/test.tfstate"]) == 0
    )
    error_message = "Provider state is independently bound; platform state must not be writable."
  }
  assert {
    condition = alltrue([for permission in [
      ["lambda:GetPolicy", "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-shared"],
      ["lambda:ListVersionsByFunction", "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-shared"],
      ["iam:ListRolePolicies", "arn:aws:iam::123456789012:role/fictional-tools-shared-lambda"],
      ["iam:ListAttachedRolePolicies", "arn:aws:iam::123456789012:role/fictional-tools-websearch-gateway"],
      ["dynamodb:DescribeTimeToLive", "arn:aws:dynamodb:us-east-1:123456789012:table/fictional-tools-operations"],
      ["events:ListTargetsByRule", "arn:aws:events:us-east-1:123456789012:rule/fictional-tools-interpreter-cleanup"],
      ["logs:DescribeLogGroups", "*"],
      ["logs:ListTagsForResource", "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/fictional-tools-shared"],
      ] : anytrue([for statement in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement :
        contains(try(tolist(statement.Action), [statement.Action]), permission[0]) &&
        contains(try(tolist(statement.Resource), [statement.Resource]), permission[1])
    ])])
    error_message = "Every configured provider resource must be refreshable under its bounded read grant."
  }
  assert {
    condition = (
      toset(flatten([for s in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement :
        flatten([s.Resource]) if contains(flatten([s.Action]), "lambda:ListVersionsByFunction")
        ])) == toset([
        "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-shared",
        "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-shared:*",
      ]) && length(aws_iam_role_policy.shared_tools_gateway) == 0
    )
    error_message = "Unselected retained Browser functions and websearch gateways must receive no refresh grant."
  }
  assert {
    condition = !anytrue([for statement in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement :
      anytrue([for action in flatten([statement.Action]) : startswith(action, "kms:")])
    ])
    error_message = "An omitted DynamoDB key binding must grant no KMS authority."
  }
  assert {
    condition = alltrue([for statement in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement :
      !contains(try(tolist(statement.Action), [statement.Action]), "dynamodb:DeleteTable") ||
      toset(statement.Resource) == toset([
        "arn:aws:dynamodb:us-east-1:123456789012:table/fictional-tools-operations",
        "arn:aws:dynamodb:us-east-1:123456789012:table/fictional-tools-browser-operations"
      ])
    ])
    error_message = "Table writes may not reach any other existing resource."
  }

  assert {
    condition = alltrue(flatten([for action in ["codebuild:BatchGetBuilds", "codebuild:StopBuild"] : [
      for resource in [
        "arn:aws:codebuild:us-east-1:123456789012:project/adp-test-shared-tools-build",
        "arn:aws:codebuild:us-east-1:123456789012:project/other-project",
        "arn:aws:codebuild:us-east-1:123456789012:build/adp-test-shared-tools-build:example"
        ] : anytrue([for s in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement :
          contains(flatten([s.Action]), action) && contains(flatten([s.Resource]), resource)
      ]) == (resource == "arn:aws:codebuild:us-east-1:123456789012:project/adp-test-shared-tools-build")
    ]]))
    error_message = "CodeBuild polling and stop require the exact project ARN, never another project or a build ARN."
  }
}

run "wrong_or_missing_state_blocks_release_role" {
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "fictional-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E12345678" }
  }
  command = plan
  variables { shared_tools_state_key = "tools/platform/test.tfstate" }
  expect_failures = [aws_iam_role.shared_tools_deployment]
}

run "dynamodb_default_key_refresh_is_metadata_only_and_exact" {
  command = plan
  variables {
    shared_tools_dynamodb_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555"
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "fictional-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E12345678" }
  }
  assert {
    condition = [for statement in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement :
      statement if anytrue([for action in flatten([statement.Action]) : startswith(action, "kms:")])
      ] == [{
        Effect   = "Allow", Action = "kms:DescribeKey",
        Resource = "arn:aws:kms:us-east-1:123456789012:key/11111111-2222-3333-4444-555555555555"
    }]
    error_message = "Default-key normalization may only describe the exact selected key, with no cryptographic, alias or write permissions."
  }
}

run "dynamodb_key_rejects_other_account" {
  command = plan
  variables {
    shared_tools_dynamodb_kms_key_arn = "arn:aws:kms:us-east-1:210987654321:key/11111111-2222-3333-4444-555555555555"
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "fictional-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E12345678" }
  }
  expect_failures = [var.shared_tools_dynamodb_kms_key_arn]
}

run "dynamodb_key_rejects_other_region" {
  command = plan
  variables {
    shared_tools_dynamodb_kms_key_arn = "arn:aws:kms:eu-west-1:123456789012:key/11111111-2222-3333-4444-555555555555"
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "fictional-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E12345678" }
  }
  expect_failures = [var.shared_tools_dynamodb_kms_key_arn]
}

run "dynamodb_key_rejects_alias_resource" {
  command = plan
  variables {
    shared_tools_dynamodb_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:alias/aws/dynamodb"
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "fictional-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E12345678" }
  }
  expect_failures = [var.shared_tools_dynamodb_kms_key_arn]
}

run "dynamodb_key_rejects_wildcard_resource" {
  command = plan
  variables {
    shared_tools_dynamodb_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/*"
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "fictional-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E12345678" }
  }
  expect_failures = [var.shared_tools_dynamodb_kms_key_arn]
}

run "release_role_is_opt_in" {
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "fictional-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E12345678" }
  }
  command = plan
  variables { enable_shared_tools_deployment = false }
  assert {
    condition     = length(aws_iam_role.shared_tools_deployment) == 0
    error_message = "No provider release identity is created before coordinator approval."
  }
}

run "dynamodb_lock_is_only_the_approved_table" {
  command = plan
  variables { shared_tools_lock_table = "fictional-existing-lock" }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "fictional-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E12345678" }
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.shared_tools_state_lock[0].policy).Statement[0].Resource == "arn:aws:dynamodb:us-east-1:123456789012:table/fictional-existing-lock" &&
      toset(jsondecode(aws_iam_role_policy.shared_tools_state_lock[0].policy).Statement[0].Action) == toset([
        "dynamodb:DescribeTable", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"
      ])
    )
    error_message = "DynamoDB locking may access only the selected existing backend table."
  }
}

run "legacy_retention_readback_excludes_worker_policy_writes" {
  command = plan
  variables {
    shared_tools_legacy_rest_api_id      = "fictionalapi"
    shared_tools_legacy_worker_role_arns = ["arn:aws:iam::123456789012:role/fictional-worker"]
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "fictional-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E12345678" }
  }
  assert {
    condition = (
      toset(flatten([for s in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement :
        flatten([s.Resource]) if contains(flatten([s.Action]), "lambda:ListVersionsByFunction")
        ])) == toset([
        "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-shared",
        "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-shared:*",
        "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-browser",
      ]) &&
      toset(one([for s in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement : s
        if s.Resource == "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-browser"
        ]).Action) == toset([
        "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:GetPolicy", "lambda:ListTags",
        "lambda:ListVersionsByFunction", "lambda:UpdateFunctionCode", "lambda:UpdateFunctionConfiguration",
      ])
    )
    error_message = "Version refresh adds only the exact retained Browser ARN; its existing write actions must not expand."
  }
  assert {
    condition = (
      anytrue([for statement in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement :
        contains(try(tolist(statement.Resource), [statement.Resource]), "arn:aws:iam::123456789012:role/fictional-worker") &&
      contains(try(tolist(statement.Action), [statement.Action]), "iam:GetRolePolicy")]) &&
      !anytrue([for statement in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement :
        contains(try(tolist(statement.Resource), [statement.Resource]), "arn:aws:iam::123456789012:role/fictional-worker") &&
      contains(try(tolist(statement.Action), [statement.Action]), "iam:PutRolePolicy")])
    )
    error_message = "Historical worker role refresh must never grant worker-policy writes."
  }
  assert {
    condition = anytrue([for statement in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement :
      contains(try(tolist(statement.Resource), [statement.Resource]), "arn:aws:apigateway:us-east-1::/restapis/fictionalapi") &&
    contains(try(tolist(statement.Action), [statement.Action]), "apigateway:GET")])
    error_message = "Legacy API readback must be scoped to the exact existing REST API."
  }
  assert {
    condition = (
      toset(flatten([for statement in jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement :
        tolist(statement.Resource) if try(statement.Action, "") == "logs:ListTagsForResource"
        ])) == toset([
        "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/fictional-tools-shared",
        "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/fictional-tools-shared:*",
        "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/fictional-tools-browser",
        "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/fictional-tools-browser:*",
      ])
    )
    error_message = "Log tag refresh must be limited to the selected shared and retained Browser groups."
  }
}

run "selected_gateway_tag_refresh_and_total_inline_quota" {
  command = plan
  variables {
    shared_tools_legacy_rest_api_id      = "fictionalapi"
    shared_tools_legacy_worker_role_arns = ["arn:aws:iam::123456789012:role/fictional-worker"]
    shared_tools_websearch_gateway_arn   = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/fictional-websearch"
    shared_tools_vpc_id                  = "vpc-0123456789abcdef0"
    shared_tools_lock_table              = "fictional-existing-lock"
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "fictional-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E12345678" }
  }
  assert {
    condition = (
      one([for s in jsondecode(aws_iam_role_policy.shared_tools_gateway[0].policy).Statement : s
      if s.Action == "bedrock-agentcore:ListTagsForResource"]).Resource == var.shared_tools_websearch_gateway_arn &&
      toset(flatten([for s in jsondecode(aws_iam_role_policy.shared_tools_gateway[0].policy).Statement : s.Action])) == toset([
        "bedrock-agentcore:GetGateway", "bedrock-agentcore:ListGatewayTargets", "bedrock-agentcore:GetGatewayTarget",
        "bedrock-agentcore:UpdateGatewayTarget", "bedrock-agentcore:ListTagsForResource",
      ])
    )
    error_message = "Gateway tag refresh must bind the selected gateway itself and preserve the existing action set."
  }
  assert {
    condition = alltrue([for case in [
      { action = "bedrock-agentcore:ListTagsForResource", resource = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/fictional-websearch", allowed = true },
      { action = "bedrock-agentcore:ListTagsForResource", resource = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/fictional-websearch/target/known", allowed = false },
      { action = "bedrock-agentcore:ListTagsForResource", resource = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/other", allowed = false },
      { action = "bedrock-agentcore:ListTagsForResource", resource = "arn:aws:bedrock-agentcore:us-east-1:999999999999:gateway/fictional-websearch", allowed = false },
      { action = "bedrock-agentcore:ListTagsForResource", resource = "arn:aws:bedrock-agentcore:us-west-2:123456789012:gateway/fictional-websearch", allowed = false },
      { action = "bedrock-agentcore:TagResource", resource = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/fictional-websearch", allowed = false },
      { action = "bedrock-agentcore:UpdateGateway", resource = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/fictional-websearch", allowed = false },
      { action = "bedrock-agentcore:CreateGateway", resource = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/fictional-websearch", allowed = false },
      { action = "bedrock-agentcore:DeleteGateway", resource = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/fictional-websearch", allowed = false },
      { action = "lambda:ListVersionsByFunction", resource = "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-shared", allowed = true },
      { action = "lambda:ListVersionsByFunction", resource = "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-browser", allowed = true },
      { action = "lambda:ListVersionsByFunction", resource = "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-browser:live", allowed = false },
      { action = "lambda:ListVersionsByFunction", resource = "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-browser-other", allowed = false },
      { action = "lambda:ListVersionsByFunction", resource = "arn:aws:lambda:us-east-1:123456789012:function:other-browser", allowed = false },
      { action = "lambda:ListVersionsByFunction", resource = "arn:aws:lambda:us-west-2:123456789012:function:fictional-tools-browser", allowed = false },
      { action = "lambda:ListVersionsByFunction", resource = "arn:aws:lambda:us-east-1:999999999999:function:fictional-tools-browser", allowed = false },
      { action = "lambda:DeleteFunction", resource = "arn:aws:lambda:us-east-1:123456789012:function:fictional-tools-browser", allowed = false },
      { action = "lambda:UpdateFunctionCode", resource = "arn:aws:lambda:us-east-1:123456789012:function:other-browser", allowed = false },
      ] : anytrue([for s in concat(
        jsondecode(aws_iam_role_policy.shared_tools_deployment[0].policy).Statement,
        jsondecode(aws_iam_role_policy.shared_tools_gateway[0].policy).Statement,
        jsondecode(aws_iam_role_policy.shared_tools_network[0].policy).Statement,
        jsondecode(aws_iam_role_policy.shared_tools_state_lock[0].policy).Statement,
        ) : s.Effect == "Allow" && anytrue([
          for action in flatten([s.Action]) : can(regex("^${replace(replace(lower(action), "*", ".*"), "?", ".")}$", lower(case.action)))
          ]) && anytrue([
          for resource in flatten([s.Resource]) : can(regex("^${replace(replace(resource, "*", ".*"), "?", ".")}$", case.resource))
      ])]) == case.allowed
    ])
    error_message = "Rendered shared policies must permit both owned function refreshes and only the selected gateway tag read, with unrelated reads and writes denied."
  }
  assert {
    condition = sum([for policy in [
      aws_iam_role_policy.shared_tools_deployment[0].policy,
      aws_iam_role_policy.shared_tools_gateway[0].policy,
      aws_iam_role_policy.shared_tools_network[0].policy,
      aws_iam_role_policy.shared_tools_state_lock[0].policy,
    ] : length(policy)]) <= 10240
    error_message = "All optional shared role inline policies together must fit IAM's aggregate 10,240-character quota."
  }
}
