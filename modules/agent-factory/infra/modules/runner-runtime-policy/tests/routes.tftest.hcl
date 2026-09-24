variables {
  account_id  = "123456789012"
  aws_region  = "us-east-1"
  name_prefix = "adp-dev-agent"
  environment = "dev"
}
run "unconfigured_transport_has_no_invoke_grant" {
  command = plan
  assert {
    condition     = !contains(flatten([for s in output.grants : s.Action]), "execute-api:Invoke")
    error_message = "Missing gateway configuration must not restore an account-wide invoke wildcard."
  }
  assert {
    condition     = !can(regex("security-agent/|security-scans-", jsonencode(output.grants)))
    error_message = "An ordinary runner must never read or alter the security ledger."
  }
}
run "wildcard_api_is_rejected" {
  command = plan
  variables { gateway_execution_arns = ["arn:aws:execute-api:us-east-1:123456789012:*/dev/POST/agent/*"] }
  expect_failures = [var.gateway_execution_arns]
}
run "wildcard_stage_is_rejected" {
  command = plan
  variables { gateway_execution_arns = ["arn:aws:execute-api:us-east-1:123456789012:abc123/*/POST/agent/*"] }
  expect_failures = [var.gateway_execution_arns]
}
run "wildcard_method_is_rejected" {
  command = plan
  variables { gateway_execution_arns = ["arn:aws:execute-api:us-east-1:123456789012:abc123/dev/*/agent/*"] }
  expect_failures = [var.gateway_execution_arns]
}
run "unrelated_routes_are_rejected" {
  command = plan
  variables { gateway_execution_arns = ["arn:aws:execute-api:us-east-1:123456789012:abc123/dev/POST/admin/*"] }
  expect_failures = [var.gateway_execution_arns]
}
