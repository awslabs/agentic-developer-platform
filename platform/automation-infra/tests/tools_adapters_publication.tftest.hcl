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
  name_prefix             = "adp-dev"
  environment             = "dev"
  aws_region              = "us-east-1"
  cluster_name            = "adp-dev-eks-cluster"
  repository              = "aws-e/adp"
  build_project_names     = ["adp-dev-gateway-build"]
  enable_tools_deployment = true
  tools_gateway_arn       = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
}
override_data {
  target = data.aws_ssm_parameter.frontend_bucket
  values = { value = "adp-dev-frontend" }
}
override_data {
  target = data.aws_ssm_parameter.frontend_cloudfront_id
  values = { value = "E1234567890" }
}

run "publication_defaults_off" {
  command = plan
  assert {
    condition = (
      length(aws_iam_policy.tools_adapters_publication) == 0 &&
      length(aws_iam_role_policy_attachment.tools_adapters_publication) == 0 &&
      length(aws_iam_policy.tools_adapters_foundation_check) == 0 &&
      length(aws_iam_role_policy_attachment.tools_adapters_foundation_check) == 0 &&
      alltrue([for action in ["s3:GetObjectVersion", "codebuild:StopBuild", "logs:FilterLogEvents"] : !contains(local.tools_actions, action)]) &&
      one([for s in local.tools_resource_denials : s if s.Sid == "DenyOtherRepositories"]).NotResource == [local.tools_repository]
    )
    error_message = "Publication is opt-in; disabled deployments retain their original authority."
  }
}

run "publication_grants_and_combined_denials_match_real_calls" {
  command = plan
  variables { enable_tools_adapters_publication = true }
  assert {
    condition = (
      one([for s in jsondecode(aws_iam_policy.tools_adapters_publication[0].policy).Statement : s if s.Sid == "AdapterBuild"]).Resource == "arn:aws:codebuild:us-east-1:123456789012:project/adp-tools-dev-adapters-build" &&
      toset(one([for s in jsondecode(aws_iam_policy.tools_adapters_publication[0].policy).Statement : s if s.Sid == "AdapterBuild"]).Action) == toset(["codebuild:StartBuild", "codebuild:StopBuild", "codebuild:BatchGetBuilds", "codebuild:BatchGetProjects"]) &&
      one([for s in jsondecode(aws_iam_policy.tools_adapters_publication[0].policy).Statement : s if s.Sid == "AdapterRepository"]).Resource == "arn:aws:ecr:us-east-1:123456789012:repository/adp-tools-dev-adapters" &&
      toset(one([for s in jsondecode(aws_iam_policy.tools_adapters_publication[0].policy).Statement : s if s.Sid == "AdapterArchiveVersions"]).Resource) == toset([
        "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-tools-dev-adapters-build/*",
        "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/artifacts/adp-tools-dev-adapters-build/*",
      ]) &&
      one([for s in jsondecode(aws_iam_policy.tools_adapters_publication[0].policy).Statement : s if s.Sid == "AdapterBuildLog"]).Resource == "arn:aws:logs:us-east-1:123456789012:log-group:/aws/codebuild/adp-tools-dev-adapters-build:*" &&
      alltrue([for policy in [aws_iam_role_policy.tools_deployment[0].policy, aws_iam_role_policy.tools_deployment_ceiling[0].policy] :
        alltrue([for action in local.tools_adapters_actions : contains(one([for s in jsondecode(policy).Statement : s if s.Sid == "DenyOtherAPIs"]).NotAction, action)])
      ]) &&
      length(aws_iam_role_policy.tools_deployment[0].policy) + length(aws_iam_role_policy.tools_deployment_ceiling[0].policy) <= 10240 &&
      length(aws_iam_policy.tools_deployment_resource_ceiling[0].policy) <= 6144 &&
      length(aws_iam_policy.tools_adapters_publication[0].policy) <= 6144
    )
    error_message = "Calls must use their real AWS authorization resources, pass both action ceilings, and fit aggregate IAM quotas."
  }
  assert {
    # Evaluate the complete rendered release policy set, not only the new Allow.
    # These actions have no conditional grants; all wildcard resources below
    # are trailing-prefix patterns, including the IAM all-resource '*'.
    condition = alltrue([for input in concat(
      [for action in ["ecr:DescribeRepositories", "ecr:DescribeImages", "ecr:BatchGetImage", "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage"] :
        { action = action, resource = local.tools_adapters_repository }
      ],
      [for action in ["codebuild:StartBuild", "codebuild:StopBuild", "codebuild:BatchGetBuilds", "codebuild:BatchGetProjects"] :
        { action = action, resource = local.tools_adapters_project }
      ],
      [for action in ["codebuild:StartBuild", "codebuild:StopBuild", "codebuild:BatchGetBuilds", "codebuild:BatchGetProjects"] :
        { action = action, resource = local.tools_adapters_check_project }
      ],
      [
        { action = "s3:PutObject", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-tools-dev-adapters-build/source.zip" },
        { action = "s3:GetObjectVersion", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-tools-dev-adapters-build/source.zip" },
        { action = "s3:GetObjectVersion", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/artifacts/adp-tools-dev-adapters-build/image.tar" },
        { action = "logs:FilterLogEvents", resource = local.tools_adapters_log_group },
        { action = "logs:FilterLogEvents", resource = local.tools_adapters_check_log },
        { action = "s3:PutObject", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-tools-dev-adapters-foundation-check/source.zip" },
        { action = "s3:GetObjectVersion", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-tools-dev-adapters-foundation-check/source.zip" },
        { action = "s3:GetObjectVersion", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/artifacts/adp-tools-dev-adapters-foundation-check/result.tar.gz" },
        { action = "codebuild:StartBuild", resource = local.tools_build_project },
        { action = "ecr:PutImage", resource = local.tools_repository },
        { action = "s3:PutObject", resource = local.tools_state_object },
      ],
      ) : alltrue([for effect in ["Allow", "Deny"] :
        anytrue([for s in concat(
          jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement,
          jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement,
          jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement,
          jsondecode(aws_iam_policy.tools_adapters_publication[0].policy).Statement,
          jsondecode(aws_iam_policy.tools_adapters_foundation_check[0].policy).Statement,
          ) : s.Effect == effect &&
          try(anytrue([for action in flatten([s.Action]) : endswith(action, "*") ? startswith(lower(input.action), lower(trimsuffix(action, "*"))) : lower(action) == lower(input.action)]), !contains(s.NotAction, input.action)) &&
          try(anytrue([for resource in flatten([s.Resource]) : endswith(resource, "*") ? startswith(input.resource, trimsuffix(resource, "*")) : input.resource == resource]), !anytrue([for resource in flatten([s.NotResource]) : endswith(resource, "*") ? startswith(input.resource, trimsuffix(resource, "*")) : input.resource == resource]))
        ]) == (effect == "Allow")
    ])])
    error_message = "Every publisher call needs an Allow and no matching Deny; existing facade and state operations must still work."
  }
  assert {
    # An independent resource-policy Allow must not bypass the explicit fences.
    condition = alltrue([for input in [
      { action = "codebuild:StartBuild", resource = "arn:aws:codebuild:us-east-1:123456789012:project/adp-tools-prod-adapters-foundation-check" },
      { action = "codebuild:UpdateProject", resource = local.tools_adapters_check_project },
      { action = "s3:PutObject", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/artifacts/adp-tools-dev-adapters-foundation-check/result.tar.gz" },
      { action = "s3:GetObject", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-tools-dev-adapters-foundation-check/source.zip" },
      { action = "s3:DeleteObject", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-tools-dev-adapters-foundation-check/source.zip" },
      { action = "s3:GetObjectVersion", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/artifacts/adp-tools-prod-adapters-foundation-check/result.tar.gz" },
      { action = "ecr:PutImage", resource = local.tools_gateway_repository },
      { action = "ecr:PutImage", resource = "arn:aws:ecr:us-east-1:123456789012:repository/adp-tools-prod-adapters" },
      { action = "ecr:PutImage", resource = "arn:aws:ecr:us-west-2:123456789012:repository/adp-tools-dev-adapters" },
      { action = "ecr:PutImage", resource = "arn:aws:ecr:us-east-1:000000000000:repository/adp-tools-dev-adapters" },
      { action = "ecr:BatchDeleteImage", resource = local.tools_adapters_repository },
      { action = "ecr:PutImageTagMutability", resource = local.tools_adapters_repository },
      { action = "ecr:GetDownloadUrlForLayer", resource = local.tools_adapters_repository },
      { action = "codebuild:StartBuild", resource = "arn:aws:codebuild:us-east-1:123456789012:project/adp-tools-prod-adapters-build" },
      { action = "codebuild:BatchGetBuilds", resource = "arn:aws:codebuild:us-west-2:123456789012:project/adp-tools-dev-adapters-build" },
      { action = "codebuild:StopBuild", resource = local.tools_build_project },
      { action = "codebuild:UpdateProject", resource = local.tools_adapters_project },
      { action = "codebuild:DeleteProject", resource = local.tools_adapters_project },
      { action = "s3:GetObjectVersion", resource = local.tools_state_object },
      { action = "s3:GetObjectVersion", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/artifacts/adp-tools-prod-adapters-build/image.tar" },
      { action = "s3:GetObjectVersion", resource = "arn:aws:s3:::adp-terraform-state-000000000000/codebuild/artifacts/adp-tools-dev-adapters-build/image.tar" },
      { action = "s3:PutObject", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/artifacts/adp-tools-dev-adapters-build/image.tar" },
      { action = "s3:PutObject", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-tools-prod-adapters-build/source.zip" },
      { action = "s3:GetObject", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-tools-dev-adapters-build/source.zip" },
      { action = "s3:GetObject", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/artifacts/adp-tools-dev-adapters-build/image.tar" },
      { action = "s3:DeleteObject", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/src/adp-tools-dev-adapters-build/source.zip" },
      { action = "s3:DeleteObjectVersion", resource = "arn:aws:s3:::adp-terraform-state-123456789012/codebuild/artifacts/adp-tools-dev-adapters-build/image.tar" },
      { action = "logs:FilterLogEvents", resource = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/codebuild/adp-tools-prod-adapters-build:*" },
      { action = "logs:FilterLogEvents", resource = "arn:aws:logs:us-east-1:000000000000:log-group:/aws/codebuild/adp-tools-dev-adapters-build:*" },
      { action = "logs:DeleteLogGroup", resource = local.tools_adapters_log_group },
      { action = "iam:PassRole", resource = "arn:aws:iam::123456789012:role/adp-tools-dev-adapters-build" },
      { action = "iam:PutRolePolicy", resource = "arn:aws:iam::123456789012:role/adp-dev-tools-trusted-deployment" },
      { action = "eks:CreateAccessEntry", resource = local.tools_cluster },
      { action = "secretsmanager:GetSecretValue", resource = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/users/example" },
      ] : anytrue([for s in concat(
        jsondecode(aws_iam_role_policy.tools_deployment[0].policy).Statement,
        jsondecode(aws_iam_role_policy.tools_deployment_ceiling[0].policy).Statement,
        jsondecode(aws_iam_policy.tools_deployment_resource_ceiling[0].policy).Statement,
        jsondecode(aws_iam_policy.tools_adapters_publication[0].policy).Statement,
        jsondecode(aws_iam_policy.tools_adapters_foundation_check[0].policy).Statement,
        ) : s.Effect == "Deny" &&
        try(anytrue([for action in flatten([s.Action]) : endswith(action, "*") ? startswith(lower(input.action), lower(trimsuffix(action, "*"))) : lower(action) == lower(input.action)]), !contains(s.NotAction, input.action)) &&
        try(anytrue([for resource in flatten([s.Resource]) : endswith(resource, "*") ? startswith(input.resource, trimsuffix(resource, "*")) : input.resource == resource]), !anytrue([for resource in flatten([s.NotResource]) : endswith(resource, "*") ? startswith(input.resource, trimsuffix(resource, "*")) : input.resource == resource])) &&
        !can(s.Condition)
    ])])
    error_message = "Other environments/accounts/regions, archive writes, destructive calls, role passing, provisioning and tenant access must be explicitly denied."
  }
}

run "production_names_are_environment_specific" {
  command = plan
  variables {
    environment                       = "prod"
    name_prefix                       = "adp-prod"
    enable_tools_adapters_publication = true
  }
  assert {
    condition = (
      local.tools_adapters_repository == "arn:aws:ecr:us-east-1:123456789012:repository/adp-tools-prod-adapters" &&
      local.tools_adapters_project == "arn:aws:codebuild:us-east-1:123456789012:project/adp-tools-prod-adapters-build" &&
      jsondecode(aws_iam_role.tools_deployment[0].assume_role_policy).Statement[0].Condition.StringEquals["token.actions.githubusercontent.com:sub"] == "repo:aws-e/adp:environment:adp-tools-deploy-prod"
    )
    error_message = "Production must get its own named resources and protected-environment trust."
  }
}

run "reject_publication_without_tools_role" {
  command = plan
  variables {
    enable_tools_deployment           = false
    enable_tools_adapters_publication = true
  }
  expect_failures = [var.enable_tools_adapters_publication]
}

run "reject_unqualified_environment" {
  command = plan
  variables {
    environment                       = "test"
    enable_tools_adapters_publication = true
  }
  expect_failures = [var.enable_tools_adapters_publication]
}

run "all_optional_tools_permissions_fit_quotas" {
  command = plan
  variables {
    enable_tools_adapters_publication = true
    tools_gateway_kms_key_arn         = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001"
    tools_inputs_secret_arn           = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/tools/inputs-ABC123"
    tools_gateway_target_arns = [for id in ["000000000001", "000000000002", "000000000003"] :
      "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example/target/${id}"
    ]
    tools_knowledge_target_creation = {
      vpc_id             = "vpc-00000000000000001"
      subnet_ids         = ["subnet-00000000000000001", "subnet-00000000000000002"]
      security_group_ids = ["sg-00000000000000001"]
    }
  }
  override_data {
    target = data.aws_subnet.tools_knowledge["subnet-00000000000000002"]
    values = { vpc_id = "vpc-00000000000000001", map_public_ip_on_launch = false, availability_zone_id = "use1-az2" }
  }
  assert {
    condition = (
      length(aws_iam_role_policy.tools_deployment[0].policy) + length(aws_iam_role_policy.tools_deployment_ceiling[0].policy) <= 10240 &&
      alltrue([for policy in [aws_iam_policy.tools_deployment_resource_ceiling[0].policy, aws_iam_policy.tools_adapters_publication[0].policy, aws_iam_policy.tools_adapters_foundation_check[0].policy, aws_iam_policy.tools_gateway_key[0].policy, aws_iam_policy.tools_knowledge_creation[0].policy] : length(policy) <= 6144])
    )
    error_message = "Combined optional features must fit IAM quotas without wildcard expansion."
  }
}
