mock_provider "aws" {
  mock_resource "aws_dynamodb_table" {
    defaults = { arn = "arn:aws:dynamodb:us-east-1:123456789012:table/offline-native-authority" }
  }
}

variables {
  table_name            = "offline-native-authority"
  gateway_role_arn      = "arn:aws:iam::123456789012:role/offline-gateway"
  registration_role_arn = "arn:aws:iam::123456789012:role/offline-registrar"
  ci_role_arns          = ["arn:aws:iam::123456789012:role/offline-native-ci"]
  execution_table_arn   = "arn:aws:dynamodb:us-east-1:123456789012:table/offline-native-executions"
}

run "protected_table_and_minimal_runtime_policy" {
  command = apply

  assert {
    condition     = aws_dynamodb_table.authority.deletion_protection_enabled && aws_dynamodb_table.authority.point_in_time_recovery[0].enabled
    error_message = "Authority and uncertain effects must be retained."
  }
  assert {
    condition     = length(aws_dynamodb_table.authority.ttl) == 0
    error_message = "Designation tombstones must never expire automatically."
  }
  assert {
    condition = (
      jsondecode(output.gateway_policy_json).Statement[1].Condition["ForAllValues:StringLike"]["dynamodb:LeadingKeys"][0] == "TENANT#*" &&
      jsondecode(output.gateway_policy_json).Statement[1].Condition.Null["dynamodb:LeadingKeys"] == "false"
    )
    error_message = "Gateway writes must require explicit tenant journal keys."
  }
  assert {
    condition = (
      jsondecode(output.gateway_policy_json).Statement[2].Action == ["dynamodb:Query"] &&
      jsondecode(output.gateway_policy_json).Statement[2].Condition["ForAllValues:StringLike"]["dynamodb:LeadingKeys"] == ["TENANT#*"] &&
      jsondecode(output.gateway_policy_json).Statement[3].Action == ["dynamodb:GetItem"] &&
      jsondecode(output.gateway_policy_json).Statement[3].Resource == var.execution_table_arn
    )
    error_message = "Status may query tenant journals and read exactly the configured facade execution table."
  }
  assert {
    condition = (
      jsondecode(aws_dynamodb_resource_policy.protection.policy).Statement[0].Effect == "Deny" &&
      contains(jsondecode(aws_dynamodb_resource_policy.protection.policy).Statement[0].Action, "dynamodb:DeleteItem") &&
      contains(jsondecode(aws_dynamodb_resource_policy.protection.policy).Statement[0].Action, "dynamodb:BatchWriteItem") &&
      contains(jsondecode(aws_dynamodb_resource_policy.protection.policy).Statement[0].Action, "dynamodb:PartiQLDelete") &&
      jsondecode(aws_dynamodb_resource_policy.protection.policy).Statement[0].Principal == "*"
    )
    error_message = "Other IAM allows must not permit deletion or a batch-delete bypass."
  }
  assert {
    condition = (
      jsondecode(aws_dynamodb_resource_policy.protection.policy).Statement[1].Condition.ArnNotEquals["aws:PrincipalArn"] == var.registration_role_arn &&
      contains(jsondecode(aws_dynamodb_resource_policy.protection.policy).Statement[1].Condition["ForAnyValue:StringLike"]["dynamodb:LeadingKeys"], "TOOLS_TEST_SUBJECT#*") &&
      contains(jsondecode(aws_dynamodb_resource_policy.protection.policy).Statement[1].Condition["ForAnyValue:StringLike"]["dynamodb:LeadingKeys"], "TOOLS_TEST_DEPLOYMENT#*") &&
      jsondecode(aws_dynamodb_resource_policy.protection.policy).Statement[3].Effect == "Deny" &&
      jsondecode(aws_dynamodb_resource_policy.protection.policy).Statement[4].Action[0] == "dynamodb:*"
    )
    error_message = "Registration requires its separate protected writer; gateway and CI alternate paths stay denied."
  }
}

run "registrar_is_not_gateway" {
  command = plan
  variables { registration_role_arn = "arn:aws:iam::123456789012:role/offline-gateway" }
  expect_failures = [aws_dynamodb_resource_policy.protection]
}

run "registrar_is_not_ci" {
  command = plan
  variables { registration_role_arn = "arn:aws:iam::123456789012:role/offline-native-ci" }
  expect_failures = [aws_dynamodb_resource_policy.protection]
}

run "reject_gateway_role_arn_star" {
  command = plan
  variables { gateway_role_arn = "arn:aws:iam::123456789012:role/offline-*" }
  expect_failures = [var.gateway_role_arn]
}

run "reject_gateway_role_arn_question" {
  command = plan
  variables { gateway_role_arn = "arn:aws:iam::123456789012:role/offline-?" }
  expect_failures = [var.gateway_role_arn]
}

run "reject_registration_role_arn_star" {
  command = plan
  variables { registration_role_arn = "arn:aws:iam::123456789012:role/offline-*" }
  expect_failures = [var.registration_role_arn]
}

run "reject_registration_role_arn_question" {
  command = plan
  variables { registration_role_arn = "arn:aws:iam::123456789012:role/offline-?" }
  expect_failures = [var.registration_role_arn]
}

run "reject_ci_role_arns_star" {
  command = plan
  variables { ci_role_arns = ["arn:aws:iam::123456789012:role/offline-*"] }
  expect_failures = [var.ci_role_arns]
}

run "reject_ci_role_arns_question" {
  command = plan
  variables { ci_role_arns = ["arn:aws:iam::123456789012:role/offline-?"] }
  expect_failures = [var.ci_role_arns]
}
