mock_provider "aws" {
  mock_data "aws_caller_identity" { defaults = { account_id = "123456789012" } }
  mock_data "aws_region" { defaults = { region = "us-east-1" } }
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/adapter-build" } }
  mock_resource "aws_ecr_repository" { defaults = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/adp-tools-dev-adapters" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/codebuild/adp-tools-dev-adapters-build" } }
}

variables {
  aws_account_id = "123456789012"
  aws_region     = "us-east-1"
  environment    = "dev"
  state_bucket   = "fictional-private-tools-source"
}

run "isolated_publisher" {
  command = apply
  assert {
    condition = (
      aws_ecr_repository.adapter.image_tag_mutability == "IMMUTABLE" &&
      aws_ecr_repository.adapter.force_delete == false &&
      aws_ecr_repository.adapter.image_scanning_configuration[0].scan_on_push &&
      aws_codebuild_project.build.source[0].buildspec == "codebuild/bs-tools-adapters-build.yml" &&
      aws_codebuild_project.build.artifacts[0].type == "NO_ARTIFACTS" &&
      aws_codebuild_project.build.environment[0].environment_variable[2].name == "ADP_ADAPTER_QUALIFY_ONLY" &&
      aws_codebuild_project.build.environment[0].environment_variable[2].value == "true" &&
      aws_codebuild_project.build.environment[0].environment_variable[3].name == "ADP_ADAPTER_ARTIFACT_BUCKET" &&
      aws_codebuild_project.build.environment[0].environment_variable[3].value == var.state_bucket
    )
    error_message = "The adapter needs an immutable, retained, independently built repository."
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.build.assume_role_policy).Statement[0].Principal.Service == "codebuild.amazonaws.com" &&
      jsondecode(aws_iam_role.build.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == var.aws_account_id &&
      jsondecode(aws_iam_role.build.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceArn"] == "arn:aws:codebuild:us-east-1:123456789012:project/adp-tools-dev-adapters-build"
    )
    error_message = "No other project or runtime identity may assume the image-build role."
  }
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.build.policy).Statement) == 4 &&
      jsondecode(aws_iam_role_policy.build.policy).Statement[0].Resource == "arn:aws:s3:::fictional-private-tools-source/codebuild/src/adp-tools-dev-adapters-build/*" &&
      toset(jsondecode(aws_iam_role_policy.build.policy).Statement[0].Action) == toset(["s3:GetObject", "s3:GetObjectVersion"]) &&
      jsondecode(aws_iam_role_policy.build.policy).Statement[1].Action == "s3:PutObject" &&
      jsondecode(aws_iam_role_policy.build.policy).Statement[1].Resource == "arn:aws:s3:::fictional-private-tools-source/codebuild/artifacts/adp-tools-dev-adapters-build/*" &&
      jsondecode(aws_iam_role_policy.build.policy).Statement[2].Effect == "Deny" &&
      jsondecode(aws_iam_role_policy.build.policy).Statement[2].Resource == "*" &&
      toset(jsondecode(aws_iam_role_policy.build.policy).Statement[2].Action) == toset(["ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload"]) &&
      alltrue([for statement in jsondecode(aws_iam_role_policy.build.policy).Statement : statement.Effect == "Deny" || alltrue([for action in flatten([statement.Action]) : !startswith(action, "ecr:")])])
    )
    error_message = "The publisher must not gain runtime, secret, source-write or other repository permissions."
  }
}

run "prod_has_separate_resources" {
  command = plan
  variables { environment = "prod" }
  assert {
    condition = (
      aws_ecr_repository.adapter.name == "adp-tools-prod-adapters" &&
      aws_codebuild_project.build.name == "adp-tools-prod-adapters-build" &&
      aws_iam_role.build.name == "adp-tools-prod-adapters-build" &&
      aws_codebuild_project.build.source[0].location == "fictional-private-tools-source/codebuild/src/adp-tools-prod-adapters-build/explicit-source-required.zip" &&
      aws_codebuild_project.foundation_check.name == "adp-tools-prod-adapters-foundation-check" &&
      aws_iam_role.foundation_check.name == "adp-tools-prod-adapters-foundation-check" &&
      aws_codebuild_project.foundation_check.source[0].location == "fictional-private-tools-source/codebuild/src/adp-tools-prod-adapters-foundation-check/explicit-source-required.zip"
    )
    error_message = "Environments must not share publisher identities, source prefixes or repositories."
  }
}

run "released_image_checks_have_no_deployment_authority" {
  command = plan
  assert {
    condition = (
      aws_codebuild_project.foundation_check.source[0].buildspec == "codebuild/bs-tools-adapters-foundation-check.yml" &&
      aws_codebuild_project.foundation_check.source[0].type == "S3" &&
      aws_codebuild_project.foundation_check.artifacts[0].type == "NO_ARTIFACTS" &&
      aws_codebuild_project.foundation_check.environment[0].privileged_mode &&
      one([for variable in aws_codebuild_project.foundation_check.environment[0].environment_variable : variable.value if variable.name == "ADP_FOUNDATION_POSTGRES_IMAGE"]) == "public.ecr.aws/docker/library/postgres@sha256:cbf42f0edc946c0bc1b8eeed62403734e1f25dc67a3491455d07a92fdb558b28" &&
      length(aws_codebuild_project.foundation_check.vpc_config) == 0 &&
      aws_codebuild_project.foundation_check.service_role == aws_iam_role.foundation_check.arn &&
      length(local.foundation_check_policy) <= 10240 &&
      jsondecode(aws_iam_role.foundation_check.assume_role_policy).Statement == [{
        Effect = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole",
        Condition = { StringEquals = {
          "aws:SourceAccount" = "123456789012",
          "aws:SourceArn"     = "arn:aws:codebuild:us-east-1:123456789012:project/adp-tools-dev-adapters-foundation-check"
        } }
      }]
    )
    error_message = "Only the exact Docker-capable isolated check project may assume its bounded read/evidence role."
  }
  assert {
    condition = alltrue([for input in concat(
      flatten([for repository in ["adp-gateway", "adp-tools-dev-adapters"] : [for action in ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"] :
        { action = action, resource = "arn:aws:ecr:us-east-1:123456789012:repository/${repository}" }
      ]]),
      [
        { action = "ecr:GetAuthorizationToken", resource = "*" },
        { action = "s3:GetObject", resource = "arn:aws:s3:::fictional-private-tools-source/codebuild/src/adp-tools-dev-adapters-foundation-check/source.zip" },
        { action = "s3:GetObjectVersion", resource = "arn:aws:s3:::fictional-private-tools-source/codebuild/src/adp-tools-dev-adapters-foundation-check/source.zip" },
        { action = "s3:PutObject", resource = "arn:aws:s3:::fictional-private-tools-source/codebuild/artifacts/adp-tools-dev-adapters-foundation-check/result.tar.gz" },
        { action = "logs:CreateLogStream", resource = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/codebuild/adp-tools-dev-adapters-foundation-check:log-stream:run" },
        { action = "logs:PutLogEvents", resource = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/codebuild/adp-tools-dev-adapters-foundation-check:log-stream:run" },
      ],
      ) : alltrue([for effect in ["Allow", "Deny"] :
        anytrue([for s in jsondecode(local.foundation_check_policy).Statement : s.Effect == effect &&
          try(contains(flatten([s.Action]), input.action), !contains(s.NotAction, input.action)) &&
          try(anytrue([for resource in flatten([s.Resource]) : endswith(resource, "*") ? startswith(input.resource, trimsuffix(resource, "*")) : input.resource == resource]), !anytrue([for resource in flatten([s.NotResource]) : endswith(resource, "*") ? startswith(input.resource, trimsuffix(resource, "*")) : input.resource == resource]))
        ]) == (effect == "Allow")
    ])])
    error_message = "Released-image reads and the check project's own source/evidence/log calls must pass every ceiling."
  }
  assert {
    condition = alltrue([for input in [
      { action = "ecr:PutImage", resource = local.foundation_check_images[0] },
      { action = "ecr:UploadLayerPart", resource = local.foundation_check_images[1] },
      { action = "ecr:GetDownloadUrlForLayer", resource = "arn:aws:ecr:us-east-1:123456789012:repository/adp-tools-prod-adapters" },
      { action = "ecr:BatchGetImage", resource = "arn:aws:ecr:us-east-1:000000000000:repository/adp-gateway" },
      { action = "ecr:BatchGetImage", resource = "arn:aws:ecr:eu-west-1:123456789012:repository/adp-gateway" },
      { action = "s3:PutObject", resource = "arn:aws:s3:::fictional-private-tools-source/codebuild/src/adp-tools-dev-adapters-foundation-check/source.zip" },
      { action = "s3:GetObject", resource = "arn:aws:s3:::fictional-private-tools-source/codebuild/artifacts/adp-tools-dev-adapters-foundation-check/result.tar.gz" },
      { action = "s3:GetObjectVersion", resource = "arn:aws:s3:::fictional-private-tools-source/codebuild/src/adp-tools-prod-adapters-foundation-check/source.zip" },
      { action = "s3:PutObject", resource = "arn:aws:s3:::fictional-private-tools-source/platform.tfstate" },
      { action = "s3:DeleteObject", resource = "arn:aws:s3:::fictional-private-tools-source/codebuild/artifacts/adp-tools-dev-adapters-foundation-check/result.tar.gz" },
      { action = "logs:PutLogEvents", resource = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/codebuild/adp-tools-dev-adapters-build:log-stream:run" },
      { action = "codebuild:StartBuild", resource = local.foundation_check_arn },
      { action = "iam:PassRole", resource = "*" },
      { action = "sts:AssumeRole", resource = "*" },
      { action = "eks:DescribeCluster", resource = "*" },
      { action = "secretsmanager:GetSecretValue", resource = "*" },
      { action = "rds-db:connect", resource = "*" },
      { action = "kms:Decrypt", resource = "*" },
      { action = "bedrock-agentcore:CreateOauth2CredentialProvider", resource = "*" },
      ] : anytrue([for s in jsondecode(local.foundation_check_policy).Statement : s.Effect == "Deny" &&
        try(contains(flatten([s.Action]), input.action), !contains(s.NotAction, input.action)) &&
        try(anytrue([for resource in flatten([s.Resource]) : endswith(resource, "*") ? startswith(input.resource, trimsuffix(resource, "*")) : input.resource == resource]), !anytrue([for resource in flatten([s.NotResource]) : endswith(resource, "*") ? startswith(input.resource, trimsuffix(resource, "*")) : input.resource == resource]))
    ])])
    error_message = "Registry writes, other projects/accounts/regions, state, deployments, role passing, provider credentials and production database access must remain denied."
  }
}

run "optional_source_key_has_no_direct_decrypt" {
  command = apply
  variables { source_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001" }
  assert {
    condition = (
      one([for s in jsondecode(local.foundation_check_policy).Statement : s if s.Sid == "ArchiveKey"]).Resource == var.source_kms_key_arn &&
      one([for s in jsondecode(local.foundation_check_policy).Statement : s if s.Sid == "ArchiveKey"]).Condition.StringEquals["kms:ViaService"] == "s3.us-east-1.amazonaws.com" &&
      one([for s in jsondecode(local.foundation_check_policy).Statement : s if s.Sid == "DenyOtherKeys"]).NotResource == var.source_kms_key_arn &&
      one([for s in jsondecode(local.foundation_check_policy).Statement : s if s.Sid == "DenyDirectKeyUse"]).Condition.StringNotEquals["kms:ViaService"] == "s3.us-east-1.amazonaws.com"
    )
    error_message = "The runtime qualifier also restricts archive key use to the exact selected key via S3."
  }
  assert {
    condition = (
      length(jsondecode(aws_iam_role_policy.build.policy).Statement) == 5 &&
      toset(jsondecode(aws_iam_role_policy.build.policy).Statement[4].Action) == toset(["kms:Decrypt", "kms:GenerateDataKey"]) &&
      jsondecode(aws_iam_role_policy.build.policy).Statement[4].Resource == var.source_kms_key_arn &&
      jsondecode(aws_iam_role_policy.build.policy).Statement[4].Condition.StringEquals["kms:ViaService"] == "s3.us-east-1.amazonaws.com" &&
      jsondecode(aws_iam_role_policy.build.policy).Statement[4].Condition.StringEquals["kms:CallerAccount"] == var.aws_account_id
    )
    error_message = "Source decryption must be restricted to the selected key via S3."
  }
}

run "wrong_provider_account_refused" {
  command = plan
  variables { aws_account_id = "111122223333" }
  expect_failures = [aws_ecr_repository.adapter]
}

run "wrong_provider_region_refused" {
  command = plan
  variables { aws_region = "eu-west-1" }
  expect_failures = [aws_ecr_repository.adapter]
}
