mock_provider "aws" {
  mock_resource "aws_iam_role" { defaults = { arn = "arn:aws:iam::123456789012:role/adp-tools-dev-interceptor" } }
  mock_resource "aws_kms_key" { defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001" } }
  mock_resource "aws_cloudwatch_log_group" { defaults = { arn = "arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/adp-tools-dev-interceptor" } }
  mock_data "aws_lambda_function" { defaults = { role = "arn:aws:iam::123456789012:role/adp-dev-shared-tools-shared-lambda" } }
}

variables {
  aws_account_id        = "123456789012"
  tools_facade_role_arn = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
  gateway_arn           = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/adp-tools-example"
}

run "default_off_does_not_install_placeholder" {
  command = plan
  assert {
    condition = (
      length(aws_lambda_function.interceptor) == 0 &&
      length(aws_iam_role.interceptor) == 0 &&
      length(aws_cloudwatch_log_group.interceptor) == 0 &&
      output.interceptor_function_name == ""
    )
    error_message = "Managed interceptor must stay off without explicit bootstrap opt-in."
  }
}

run "needs_facade_identity" {
  command = plan
  variables { enable_interceptor = true }
  expect_failures = [var.enable_interceptor]
}

run "refuses_dual_function_ownership" {
  command = plan
  variables {
    enable_interceptor             = true
    eks_oidc_provider_arn          = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/A298CB7090905D7235ACFF04EC05F630"
    interceptor_lambda_arn         = "arn:aws:lambda:us-east-1:123456789012:function:legacy-interceptor"
    interceptor_execution_role_arn = "arn:aws:iam::123456789012:role/legacy-interceptor"
  }
  expect_failures = [var.enable_interceptor]
}

run "managed_placeholder_has_only_verification_and_log_grants" {
  command = apply
  variables {
    enable_interceptor    = true
    eks_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/A298CB7090905D7235ACFF04EC05F630"
  }
  assert {
    condition = (
      aws_lambda_function.interceptor[0].function_name == "adp-tools-dev-interceptor" &&
      aws_lambda_function.interceptor[0].handler == "tools_interceptor.handler.handler" &&
      aws_lambda_function.interceptor[0].reserved_concurrent_executions == -1 &&
      !aws_lambda_function.interceptor[0].publish &&
      aws_lambda_function.interceptor[0].environment[0].variables["ADP_TOOLS_FACADE_ROLE_ARN"] == var.tools_facade_role_arn &&
      aws_lambda_function.interceptor[0].environment[0].variables["ADP_TOOLS_ASSERTION_KEY_ARN"] == aws_kms_key.assertion[0].arn &&
      jsondecode(aws_kms_key.assertion[0].policy).Statement[2].Principal.AWS == aws_iam_role.interceptor[0].arn &&
      jsondecode(aws_iam_role.interceptor[0].assume_role_policy).Statement[0].Principal.Service == "lambda.amazonaws.com" &&
      jsondecode(aws_iam_role_policy.interceptor[0].policy).Statement[0].Resource == aws_kms_key.assertion[0].arn &&
      toset(flatten([for statement in jsondecode(aws_iam_role_policy.interceptor[0].policy).Statement : statement.Action])) == toset(["kms:Verify", "kms:DescribeKey", "logs:CreateLogStream", "logs:PutLogEvents"]) &&
      length(aws_iam_role_policy.gateway_interceptor) == 1 &&
      jsondecode(aws_iam_role_policy.gateway_interceptor[0].policy).Statement[0].Resource == "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-dev-interceptor:live"
    )
    error_message = "Bootstrap must grant invoke only to the future live alias without attaching the placeholder."
  }
}

run "shared_tools_verification_is_only_added_by_bootstrap_after_publication" {
  command = apply
  variables {
    enable_interceptor             = true
    eks_oidc_provider_arn          = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/A298CB7090905D7235ACFF04EC05F630"
    interceptor_lambda_arn         = "arn:aws:lambda:us-east-1:123456789012:function:adp-tools-dev-interceptor:live"
    interceptor_execution_role_arn = "arn:aws:iam::123456789012:role/adp-tools-dev-interceptor"
  }
  assert {
    condition = (
      jsondecode(aws_iam_role_policy.gateway_interceptor[0].policy).Statement[0].Resource == var.interceptor_lambda_arn &&
      jsondecode(aws_kms_key.assertion[0].policy).Statement[3].Principal.AWS == data.aws_lambda_function.shared_tools[0].role &&
      jsondecode(aws_kms_key.assertion[0].policy).Statement[2].Principal.AWS == aws_iam_role.interceptor[0].arn &&
      jsondecode(aws_kms_key.reconciler[0].policy).Statement[1].Principal.AWS == "arn:aws:iam::123456789012:role/adp-dev-role-tools-reconciler" &&
      jsondecode(aws_kms_key.reconciler[0].policy).Statement[2].Principal.AWS == data.aws_lambda_function.shared_tools[0].role &&
      jsondecode(aws_iam_role_policy.reconciler_provider[0].policy).Statement[0].Resource == aws_kms_key.reconciler[0].arn &&
      jsondecode(aws_iam_role_policy.reconciler_provider[0].policy).Statement[1].Resource == "arn:aws:lambda:us-east-1:123456789012:function:adp-dev-shared-tools-shared" &&
      jsondecode(aws_iam_role_policy.reconciler_provider[0].policy).Statement[3].Resource == "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/outcome-repair"
    )
    error_message = "Only bootstrap grants the reviewed shared target verification key after real publication."
  }
}

run "dev_capacity_overlay_bounds_existing_function_without_publishing" {
  command = plan
  variables {
    enable_interceptor                         = true
    eks_oidc_provider_arn                      = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/A298CB7090905D7235ACFF04EC05F630"
    interceptor_reserved_concurrent_executions = jsondecode(file("../../../../environments/dev/modules/tools-bootstrap.tfvars.json")).interceptor_reserved_concurrent_executions
  }
  assert {
    condition = (
      aws_lambda_function.interceptor[0].reserved_concurrent_executions == 10 &&
      !aws_lambda_function.interceptor[0].publish &&
      aws_lambda_function.interceptor[0].function_name == "adp-tools-dev-interceptor"
    )
    error_message = "The dev overlay must reserve ten existing interceptor slots without publishing a new version."
  }
}

run "production_default_remains_unreserved" {
  command = plan
  variables {
    environment           = "prod"
    enable_interceptor    = true
    tools_facade_role_arn = "arn:aws:iam::123456789012:role/adp-prod-role-tools-facade"
    eks_oidc_provider_arn = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/A298CB7090905D7235ACFF04EC05F630"
  }
  assert {
    condition     = aws_lambda_function.interceptor[0].reserved_concurrent_executions == -1
    error_message = "Reusing this module must preserve unreserved production concurrency by default."
  }
}

run "reject_zero_that_would_disable_tool_access" {
  command = plan
  variables {
    enable_interceptor                         = true
    eks_oidc_provider_arn                      = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/A298CB7090905D7235ACFF04EC05F630"
    interceptor_reserved_concurrent_executions = 0
  }
  expect_failures = [var.interceptor_reserved_concurrent_executions]
}

run "reject_fractional_capacity" {
  command = plan
  variables {
    enable_interceptor                         = true
    eks_oidc_provider_arn                      = "arn:aws:iam::123456789012:oidc-provider/oidc.eks.us-east-1.amazonaws.com/id/A298CB7090905D7235ACFF04EC05F630"
    interceptor_reserved_concurrent_executions = 1.5
  }
  expect_failures = [var.interceptor_reserved_concurrent_executions]
}
