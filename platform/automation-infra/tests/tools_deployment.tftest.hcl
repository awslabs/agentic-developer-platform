mock_provider "external" {
  mock_data "external" { defaults = { result = { verified = "true" } } }
}
mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_iam_openid_connect_provider" {
    defaults = { arn = "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com" }
  }
  mock_data "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000" }
  }
  mock_data "aws_ssm_parameter" { defaults = { value = "mock" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
}

variables {
  name_prefix             = "adp-test"
  environment             = "test"
  aws_region              = "us-east-1"
  cluster_name            = "adp-test-eks-cluster"
  repository              = "aws-e/adp"
  build_project_names     = ["adp-test-gateway-build"]
  enable_tools_deployment = true
  tools_gateway_arn       = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
  tools_gateway_target_arns = [
    "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example/target/known-target"
  ]
}

run "release_profile_is_resource_scoped" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  assert {
    condition = jsondecode(aws_iam_role.tools_deployment[0].assume_role_policy).Statement[0].Condition.StringEquals == {
      "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
      "token.actions.githubusercontent.com:sub" = "repo:aws-e/adp:environment:adp-tools-deploy-test"
    }
    error_message = "Only the protected tools environment may assume this role."
  }
  assert {
    condition = alltrue([for subject in [
      "repo:aws-e/adp:ref:refs/heads/main",
      "repo:aws-e/adp:environment:adp-deploy-test",
      "repo:aws-e/adp:environment:adp-tools-deploy-prod",
    ] : jsondecode(aws_iam_role.tools_deployment[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] != subject])
    error_message = "Main ref, generic deployment and other tools environments must not be trusted."
  }
  assert {
    condition = (
      toset(aws_eks_access_entry.tools_deployment[0].kubernetes_groups) == toset(["adp:tools-release-test"]) &&
      aws_eks_access_entry.tools_deployment[0].type == "STANDARD"
    )
    error_message = "Kubernetes authorization must use a dedicated RBAC group, not an EKS edit policy."
  }
  assert {
    condition = (
      length(aws_iam_role_policy.tools_deployment[0].policy) <= 10240 &&
      length(aws_iam_role_policy.tools_deployment_ceiling[0].policy) <= 10240 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
      if statement.Effect == "Allow" && statement.Resource == "*"]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
      if statement.Sid == "CallerIdentity" && toset(statement.Action) == toset(["sts:GetCallerIdentity", "ecr:GetAuthorizationToken"])]) == 1
    )
    error_message = "Only non-resource-scopable identity and ECR authentication may use Resource:*."
  }
  assert {
    condition = (
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
      if statement.Sid == "ReleaseState" && statement.Resource == "arn:aws:s3:::adp-terraform-state-123456789012/tools/platform/test.tfstate"]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
      if statement.Sid == "ReleaseStateLock" && statement.Resource == "arn:aws:s3:::adp-terraform-state-123456789012/tools/platform/test.tfstate.tflock"]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
      if statement.Sid == "GatewayConfiguration" && statement.Resource == var.tools_gateway_arn]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
      if statement.Sid == "TargetConfiguration" && toset(statement.Resource) == toset(concat([var.tools_gateway_arn], var.tools_gateway_target_arns))]) == 1
    )
    error_message = "State and AgentCore access must bind exact selected resources."
  }
  assert {
    condition = (
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
      if statement.Sid == "DenyIdentityMutation" && contains(statement.Action, "sts:AssumeRole")]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
      if statement.Sid == "DenyKeyMutation" && contains(statement.Action, "kms:PutKeyPolicy")]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
      if statement.Sid == "DenyOtherRepositories" && toset(statement.NotResource) == toset([local.tools_repository])]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
      if statement.Sid == "DenyOtherFunctions" && toset(statement.NotResource) == toset([local.tools_function, "${local.tools_function}:*", local.tools_shared_function, "${local.tools_shared_function}:*"])]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
      if statement.Sid == "DenyOtherGatewayResources" && toset(statement.NotResource) == toset(local.tools_gateway_resources)]) == 1 &&
      !contains(local.tools_actions, "iam:PutRolePolicy") &&
      !contains(local.tools_actions, "kms:PutKeyPolicy") &&
      !contains(local.tools_actions, "bedrock-agentcore:CreateGateway")
    )
    error_message = "IAM/KMS mutation and writes to other repositories/functions/gateways must remain denied."
  }
  assert {
    condition = (
      toset(local.tools_ssm_release) == toset([for name in [
        "gateway-id", "gateway-arn", "gateway-url", "kms-key-arn", "caller-role-arn",
        "target-role-arn", "targets", "assertion-signing-key-arn", "execution-table-name",
        "shared-tools-lambda-arn", "release",
      ] : "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/tools/${name}"]) &&
      toset(local.tools_ssm_read) == toset(concat(local.tools_ssm_release, [
        "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/tools/reconciler-signing-key-arn",
        "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/internal-plane-alb-dns",
        "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/gateway/apigw-invoke-url",
      ])) &&
      toset(one([for statement in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : statement if statement.Sid == "ReadReleaseInputs"]).Resource) == toset(local.tools_ssm_read_only) &&
      toset(one([for statement in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : statement if statement.Sid == "PublishReleaseContext"]).Resource) == toset(local.tools_ssm_release) &&
      toset(one([for statement in jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement : statement if statement.Sid == "DenyOtherParameterReads"]).NotResource) == toset(local.tools_ssm_read) &&
      toset(one([for statement in jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement : statement if statement.Sid == "DenyOtherParameterWrites"]).Resource) == toset(local.tools_ssm_read_only) &&
      !contains(local.tools_ssm_release, "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/tools/reconciler-signing-key-arn") &&
      !contains(local.tools_ssm_read, "arn:aws:ssm:us-east-1:123456789012:parameter/adp/prod/tools/shared-tools-lambda-arn")
    )
    error_message = "Only exact selected-environment tools parameters may be read or written; the cleanup key is read-only and other environments are denied."
  }
}

run "gateway_update_passes_only_existing_role_to_agentcore" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  assert {
    condition = (
      one([for s in local.tools_resource_grants : s if s.Sid == "GatewayPassRole"]) == {
        Sid       = "GatewayPassRole", Effect = "Allow", Action = ["iam:PassRole"],
        Resource  = "arn:aws:iam::123456789012:role/adp-tools-test-gateway",
        Condition = { StringEquals = { "iam:PassedToService" = "bedrock-agentcore.amazonaws.com" } }
      } &&
      one([for s in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : s if s.Sid == "DenyOtherPassedRoles"]).NotResource == local.tools_gateway_role &&
      one([for s in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : s if s.Sid == "DenyOtherGatewayRoleService"]) == {
        Sid       = "DenyOtherGatewayRoleService", Effect = "Deny", Action = "iam:PassRole", Resource = local.tools_gateway_role,
        Condition = { StringNotEquals = { "iam:PassedToService" = "bedrock-agentcore.amazonaws.com" } }
      } &&
      alltrue([for policy in [aws_iam_role_policy.tools_deployment[0].policy, aws_iam_role_policy.tools_deployment_ceiling[0].policy] :
        [for action in one([for s in jsondecode(policy).Statement : s if s.Sid == "DenyOtherAPIs"]).NotAction : action if startswith(action, "iam:")] == ["iam:PassRole"]
      ])
    )
    error_message = "Only the existing gateway role may be passed, only to AgentCore; every other IAM action must stay explicitly denied."
  }
}

run "evaluation_gateway_image_read_does_not_allow_other_reads_or_writes" {
  command = plan
  override_resource {
    target          = aws_iam_policy.tools_deployment_resource_ceiling[0]
    override_during = plan
    values          = { arn = "arn:aws:iam::123456789012:policy/adp-test-tools-release-resource-ceiling" }
  }
  variables {
    tools_gateway_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-0000000000"
    tools_gateway_target_arns = [
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-0000000000/target/000000000001",
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-0000000000/target/000000000002",
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-0000000000/target/000000000003"
    ]
    tools_inputs_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/tools/inputs-000000"
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  assert {
    condition = (
      length(aws_iam_role_policy.tools_deployment[0].policy) + length(aws_iam_role_policy.tools_deployment_ceiling[0].policy) <= 10240 &&
      length(aws_iam_policy.tools_deployment_resource_ceiling[0].policy) <= 6144 &&
      one([for s in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : s if s.Sid == "DenyOtherAPIs"]) == one(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement) &&
      aws_iam_role_policy_attachment.tools_deployment_resource_ceiling[0].policy_arn == aws_iam_policy.tools_deployment_resource_ceiling[0].arn &&
      one([for s in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : s if s.Sid == "GatewayImageRead"]).Resource == "arn:aws:ecr:us-east-1:123456789012:repository/adp-gateway" &&
      toset(one([for s in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : s if s.Sid == "GatewayImageRead"]).Action) == toset(["ecr:DescribeImages"]) &&
      one([for s in concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement) : s if s.Sid == "DenyOtherImageReads"]).Action == "ecr:DescribeImages" &&
      toset(one([for s in concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement) : s if s.Sid == "DenyOtherImageReads"]).NotResource) == toset([local.tools_repository, local.tools_gateway_repository]) &&
      contains(one([for s in concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement) : s if s.Sid == "DenyOtherAPIs"]).NotAction, "ecr:DescribeImages")
    )
    error_message = "Only gateway/facade image metadata reads may cross the ECR ceiling; aggregate inline size is ${length(aws_iam_role_policy.tools_deployment[0].policy) + length(aws_iam_role_policy.tools_deployment_ceiling[0].policy)}."
  }
  assert {
    condition = (
      !contains(one([for s in concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement) : s if s.Sid == "DenyOtherRepositories"]).Action, "ecr:DescribeImages") &&
      toset(one([for s in concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement) : s if s.Sid == "DenyOtherRepositories"]).NotResource) == toset([local.tools_repository]) &&
      toset(one([for s in concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement) : s if s.Sid == "DenyOtherRepositories"]).Action) == toset([
        "ecr:DescribeRepositories", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability",
        "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"
      ])
    )
    error_message = "Gateway image pull/publication and every other repository's read/write access must remain explicitly denied."
  }

  assert {
    condition = alltrue(flatten([for action in ["ssm:GetParameter", "ssm:PutParameter"] : [
      for resource in concat(local.tools_ssm_read, [
        "arn:aws:ssm:us-east-1:123456789012:parameter/adp/test/tools/foreign",
        "arn:aws:ssm:us-east-1:123456789012:parameter/adp/other/tools/release",
        "arn:aws:ssm:us-east-1:000000000000:parameter/adp/test/tools/release"
        ]) : (
        anytrue([for s in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement :
          try(contains(flatten([s.Action]), action), false) && contains(flatten([s.Resource]), resource)
        ]) == contains(action == "ssm:GetParameter" ? local.tools_ssm_read : local.tools_ssm_release, resource) &&
        anytrue([for s in jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement :
          contains(flatten([s.Action]), action) && try(!contains(flatten([s.NotResource]), resource), contains(flatten([s.Resource]), resource))
        ]) == !contains(action == "ssm:GetParameter" ? local.tools_ssm_read : local.tools_ssm_release, resource)
      )
    ]]))
    error_message = "SSM compaction must preserve each exact allow and explicit deny, including read-only inputs and foreign keys/accounts/environments."
  }
  assert {
    condition = alltrue([for resource in [
      local.tools_build_project,
      "arn:aws:codebuild:us-east-1:123456789012:project/other-project",
      "arn:aws:codebuild:us-east-1:123456789012:build/adp-tools-test-facade-build:example"
      ] : anytrue([for s in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement :
        try(contains(flatten([s.Action]), "codebuild:BatchGetBuilds"), false) && contains(flatten([s.Resource]), resource)
    ]) == (resource == local.tools_build_project)])
    error_message = "Facade build polling authorizes the exact owning project; neither another project nor a build ARN grants access."
  }
}

run "first_interceptor_release_can_create_only_its_own_alias" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  assert {
    condition = (
      contains(one([for statement in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : statement if statement.Sid == "InterceptorCode"]).Action, "lambda:CreateAlias") &&
      toset(one([for statement in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : statement if statement.Sid == "InterceptorCode"]).Resource) == toset([
        "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-test-interceptor",
        "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-test-interceptor:*",
      ]) &&
      contains(one([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement) : statement if statement.Sid == "DenyOtherAPIs"]).NotAction, "lambda:CreateAlias")
    )
    error_message = "The first real release must be allowed to create an alias for only the designated interceptor, including through the action ceiling."
  }
  assert {
    condition = (
      one([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement) : statement if statement.Sid == "DenyOtherFunctions"]).Effect == "Deny" &&
      one([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement) : statement if statement.Sid == "DenyOtherFunctions"]).Action == "lambda:*" &&
      toset(one([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement) : statement if statement.Sid == "DenyOtherFunctions"]).NotResource) == toset([
        "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-test-interceptor",
        "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-test-interceptor:*",
        "arn:aws:lambda:us-east-1:123456789012:function:adp-test-shared-tools-shared",
        "arn:aws:lambda:us-east-1:123456789012:function:adp-test-shared-tools-shared:*",
      ]) &&
      contains(one([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement) : statement if statement.Sid == "DenySharedToolsWrites"]).Action, "lambda:UpdateFunctionCode") &&
      !contains(one([for statement in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : statement if statement.Sid == "SharedToolsRead"]).Action, "lambda:UpdateFunctionCode")
    )
    error_message = "Alias creation and all other Lambda actions on other functions must remain explicitly denied."
  }
}

run "reject_missing_gateway_selection" {
  command = plan
  variables {
    tools_gateway_arn         = ""
    tools_gateway_target_arns = []
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  expect_failures = [aws_iam_role_policy.tools_deployment[0]]
}

run "reject_foreign_target" {
  command = plan
  variables {
    tools_gateway_target_arns = ["arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/other/target/known-target"]
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  expect_failures = [var.tools_gateway_target_arns]
}

run "deny_foreign_resources_and_policy_mutation" {
  command = plan
  variables {
    tools_inputs_secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test/tools/inputs-ABC123"
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  assert {
    condition = (
      length(aws_iam_role_policy.tools_deployment[0].policy) <= 10240 &&
      length(aws_iam_role_policy.tools_deployment_ceiling[0].policy) <= 10240 &&
      alltrue([for action in ["iam:CreateRole", "iam:PutRolePolicy", "iam:UpdateAssumeRolePolicy", "sts:AssumeRole", "kms:PutKeyPolicy", "kms:CreateKey"] :
        !contains(local.tools_actions, action)
      ]) &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
        if statement.Sid == "DenyIdentityMutation" && statement.Effect == "Deny" && contains(statement.Action, "sts:AssumeRole")
      ]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
        if statement.Sid == "DenyKeyMutation" && statement.Effect == "Deny" && contains(statement.Action, "kms:PutKeyPolicy")
      ]) == 1
    )
    error_message = "IAM mutation, chaining and KMS policy changes must be explicitly denied."
  }
  assert {
    condition = (
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
        if statement.Sid == "DenyOtherRepositories" && statement.Effect == "Deny" && !contains(statement.NotResource, "arn:aws:ecr:us-east-1:123456789012:repository/other")
      ]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
        if statement.Sid == "DenyOtherFunctions" && statement.Effect == "Deny" && !contains(statement.NotResource, "arn:aws:lambda:us-east-1:123456789012:function:other")
      ]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
        if statement.Sid == "DenyOtherGatewayResources" && statement.Effect == "Deny" && !contains(statement.NotResource, "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/other")
      ]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
        if statement.Sid == "DenyOtherStateAndSources" && statement.Effect == "Deny" && !contains(statement.NotResource, "arn:aws:s3:::adp-terraform-state-123456789012/tools/bootstrap/test.tfstate")
      ]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
        if statement.Sid == "DenyOtherSecrets" && statement.Effect == "Deny" && statement.NotResource == var.tools_inputs_secret_arn
      ]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
        if statement.Sid == "DenyReleaseStateDeletion" && statement.Effect == "Deny" && statement.Resource == local.tools_state_object
      ]) == 1 &&
      length([for statement in concat(jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement, concat(jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement, jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement)) : statement
        if statement.Sid == "DenyGatewayAndTargetDeletion" && statement.Effect == "Deny" && contains(statement.Action, "bedrock-agentcore:DeleteGateway")
      ]) == 1
    )
    error_message = "Other repositories, functions, gateways, bootstrap state and secrets must be denied."
  }
}

run "tools_deployment_is_absent_by_default" {
  command = plan
  variables {
    enable_tools_deployment   = false
    tools_gateway_arn         = ""
    tools_gateway_target_arns = []
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  assert {
    condition     = length(aws_iam_role.tools_deployment) == 0 && length(aws_iam_role_policy.tools_deployment_ceiling) == 0 && length(aws_eks_access_entry.tools_deployment) == 0 && output.tools_deployment_role_arn == null
    error_message = "The tools deployment profile must remain opt-in."
  }
}


run "lambda_environment_decryption_is_exact_and_both_action_ceilings_allow_it" {
  command = plan
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  assert {
    condition = (
      data.aws_kms_key.tools_lambda_environment[0].key_id == "alias/aws/lambda" &&
      one([for s in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : s if s.Sid == "ToolsEnvironmentRead"]) == {
        Sid      = "ToolsEnvironmentRead", Effect = "Allow", Action = ["kms:Decrypt"],
        Resource = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000",
        Condition = { StringEquals = {
          "kms:ViaService"    = "lambda.us-east-1.amazonaws.com",
          "kms:CallerAccount" = "123456789012",
          "kms:EncryptionContext:aws:lambda:FunctionArn" = [
            "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-test-interceptor",
            "arn:aws:lambda:us-east-1:123456789012:function:adp-test-shared-tools-shared",
          ]
        } }
      } &&
      alltrue([for policy in [aws_iam_role_policy.tools_deployment[0].policy, aws_iam_role_policy.tools_deployment_ceiling[0].policy] :
        contains(one([for s in jsondecode(policy).Statement : s if s.Sid == "DenyOtherAPIs"]).NotAction, "kms:Decrypt")
      ]) &&
      alltrue([for action in ["kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey", "kms:Sign", "kms:CreateGrant", "kms:PutKeyPolicy"] :
        !contains(local.tools_actions, action)
      ])
    )
    error_message = "Both action ceilings must permit only the exact Lambda environment decrypt grant; no key mutation, signing, or discovery authority."
  }
  assert {
    # Each denied dimension is tested alone. This also models an independent
    # resource-policy Allow, as on aws/lambda: explicit identity Deny must win.
    condition = alltrue([for input in [
      { key = local.tools_lambda_environment_key, service = "lambda.us-east-1.amazonaws.com", function = local.tools_function, deny = false },
      { key = local.tools_lambda_environment_key, service = "", function = local.tools_function, deny = true },
      { key = local.tools_lambda_environment_key, service = "secretsmanager.us-east-1.amazonaws.com", function = local.tools_function, deny = true },
      { key = local.tools_lambda_environment_key, service = "lambda.us-west-2.amazonaws.com", function = local.tools_function, deny = true },
      { key = local.tools_lambda_environment_key, service = "lambda.us-east-1.amazonaws.com", function = "", deny = true },
      { key = local.tools_lambda_environment_key, service = "lambda.us-east-1.amazonaws.com", function = local.tools_shared_function, deny = false },
      { key = local.tools_lambda_environment_key, service = "", function = local.tools_shared_function, deny = true },
      { key = local.tools_lambda_environment_key, service = "lambda.us-east-1.amazonaws.com", function = "${local.tools_shared_function}:12", deny = true },
      { key = local.tools_lambda_environment_key, service = "lambda.us-east-1.amazonaws.com", function = "arn:aws:lambda:us-east-1:123456789012:function:other-function", deny = true },
      { key = local.tools_lambda_environment_key, service = "lambda.us-east-1.amazonaws.com", function = "${local.tools_function}:12", deny = true },
      { key = local.tools_lambda_environment_key, service = "lambda.us-east-1.amazonaws.com", function = "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-prod-interceptor", deny = true },
      { key = local.tools_lambda_environment_key, service = "lambda.us-east-1.amazonaws.com", function = "arn:aws:lambda:us-east-1:000000000000:function:adp-tools-test-interceptor", deny = true },
      { key = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111", service = "lambda.us-east-1.amazonaws.com", function = local.tools_function, deny = true },
      { key = "arn:aws:kms:us-west-2:123456789012:key/00000000-0000-0000-0000-000000000000", service = "lambda.us-east-1.amazonaws.com", function = local.tools_function, deny = true },
      { key = "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-000000000000", service = "lambda.us-east-1.amazonaws.com", function = local.tools_function, deny = true },
      ] : anytrue([
        for s in jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement : (
          s.Effect == "Deny" && try(s.Action == "kms:Decrypt", false) &&
          try(!contains(flatten([s.NotResource]), input.key), s.Resource == "*" || s.Resource == input.key) &&
          try(alltrue([for key, expected in s.Condition.StringNotEquals :
            !contains(flatten([expected]), lookup({ "kms:ViaService" = input.service, "kms:EncryptionContext:aws:lambda:FunctionArn" = input.function }, key, ""))
          ]), true)
        )
      ]) == input.deny
    ])
    error_message = "AWS-managed key-policy access must not bypass the exact key, Lambda service, region/account or exact interceptor/shared provider encryption contexts."
  }
  assert {
    condition = (
      length(aws_iam_role_policy.tools_deployment[0].policy) + length(aws_iam_role_policy.tools_deployment_ceiling[0].policy) <= 10240 &&
      length(aws_iam_policy.tools_deployment_resource_ceiling[0].policy) <= 6144
    )
    error_message = "The environment-read fix must fit existing IAM quotas without widening resource ceilings."
  }
}

run "disabled_tools_release_does_not_resolve_or_grant_lambda_key" {
  command = plan
  variables {
    enable_tools_deployment = false
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_bucket
    values = { value = "adp-test-frontend" }
  }
  override_data {
    target = data.aws_ssm_parameter.frontend_cloudfront_id
    values = { value = "E1234567890" }
  }
  assert {
    condition     = length(data.aws_kms_key.tools_lambda_environment) == 0 && length(aws_iam_role_policy.tools_deployment) == 0
    error_message = "Disabled tools deployment must neither resolve a missing Lambda key nor grant environment access."
  }
}
