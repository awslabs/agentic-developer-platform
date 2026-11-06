mock_provider "aws" {
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/test" } }
  mock_resource "aws_ecr_repository" { defaults = { arn = "arn:aws:ecr:us-east-1:123456789012:repository/adp-tools-facade" } }
  mock_resource "aws_kms_key" { defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001" } }
}

variables {
  aws_account_id        = "123456789012"
  tools_facade_role_arn = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
  gateway_arn           = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
}

run "bootstrap_owns_foundation" {
  command = plan
  assert {
    condition = (
      aws_ecr_repository.facade.name == "adp-tools-facade" &&
      aws_ecr_repository.facade.image_tag_mutability == "IMMUTABLE" &&
      aws_dynamodb_table.executions.point_in_time_recovery[0].enabled &&
      aws_dynamodb_table.executions.ttl[0].attribute_name == "retention_ttl" &&
      aws_kms_key.gateway.enable_key_rotation &&
      aws_iam_role.gateway.name == "adp-tools-dev-gateway" &&
      jsondecode(aws_iam_role.gateway.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == var.aws_account_id
    )
    error_message = "IAM, KMS, ECR and durable state belong to reviewed bootstrap."
  }
}

run "reject_wrong_gateway_reference" {
  command = plan
  variables { gateway_arn = "arn:aws:bedrock-agentcore:us-east-1:999999999999:gateway/adp-tools-example" }
  expect_failures = [var.gateway_arn]
}

run "reject_mismatched_broker_stage" {
  command = plan
  variables {
    credential_target_url      = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/other"
    credential_target_api_arn  = "arn:aws:execute-api:us-east-1:123456789012:fixtureapi/dev"
    credential_broker_role_arn = "arn:aws:iam::123456789012:role/fixture-gateway-service"
  }
  expect_failures = [var.credential_target_url]
}

run "broker_invocation_is_scoped_to_four_routes" {
  command = plan
  variables {
    credential_target_url      = "https://fixtureapi.execute-api.us-east-1.amazonaws.com/dev"
    credential_target_api_arn  = "arn:aws:execute-api:us-east-1:123456789012:fixtureapi/dev"
    credential_broker_role_arn = "arn:aws:iam::123456789012:role/fixture-gateway-service"
  }
  assert {
    condition = toset(jsondecode(aws_iam_role_policy.credential_target[0].policy).Statement[0].Resource) == toset([
      "${var.credential_target_api_arn}/GET/internal/v1/user-credentials",
      "${var.credential_target_api_arn}/POST/internal/v1/proxy-request",
      "${var.credential_target_api_arn}/POST/internal/v1/credential-assume-role",
      "${var.credential_target_api_arn}/POST/internal/v1/credential-materialize",
    ])
    error_message = "Gateway IAM role must invoke only the four broker routes at the selected stage."
  }
}
run "facade_build_has_exact_repository" {
  command = apply
  variables {
    eks_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/A298CB7090905D7235ACFF04EC05F630"
    state_bucket          = "adp-test-state"
  }
  assert {
    condition = (
      aws_iam_role.facade[0].name == "adp-dev-role-tools-facade" &&
      jsondecode(aws_iam_role_policy.facade[0].policy).Statement[0].Resource == var.gateway_arn &&
      jsondecode(aws_iam_role_policy.facade_build[0].policy).Statement[3].Resource == aws_ecr_repository.facade.arn &&
      aws_codebuild_project.facade_build[0].name == "adp-tools-dev-facade-build"
    )
    error_message = "Bootstrap pre-creates only the named façade identity and build project."
  }
}

run "facade_can_invoke_only_required_authority_routes" {
  command = plan
  variables {
    eks_oidc_provider_arn              = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/A298CB7090905D7235ACFF04EC05F630"
    gateway_internal_api_execution_arn = "arn:aws:execute-api:us-east-1:123456789012:fixtureapi/dev"
  }
  assert {
    condition = toset(jsondecode(aws_iam_role_policy.facade[0].policy).Statement[1].Resource) == toset([
      "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/authorize",
      "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/native-dispatch",
      "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/l5-list",
      "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/l5-call",
    ])
    error_message = "Facade discovery and invocation need the exact L5 routes while other methods and paths stay unauthorized."
  }
}
