mock_provider "aws" {}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "123456789012"
    arn        = "arn:aws:sts::123456789012:assumed-role/installation-operator/session"
    user_id    = "AROAAAAAAAAAAAAAAAAAA:session"
  }
}
override_data {
  target = data.aws_iam_role.operator
  values = {
    arn       = "arn:aws:iam::123456789012:role/installation-operator"
    unique_id = "AROAAAAAAAAAAAAAAAAAA"
  }
}
override_data {
  target = data.aws_iam_role.provider
  values = {
    arn       = "arn:aws:iam::123456789012:role/adp-dev-spp-01234567890123456789012345678901"
    unique_id = "AROABBBBBBBBBBBBBBBBB"
  }
}

variables {
  environment         = "dev"
  region              = "us-east-1"
  account_id          = "123456789012"
  operator_role_arn   = "arn:aws:iam::123456789012:role/installation-operator"
  operator_role_id    = "AROAAAAAAAAAAAAAAAAAA"
  provider_role_arn   = "arn:aws:iam::123456789012:role/adp-dev-spp-01234567890123456789012345678901"
  provider_role_id    = "AROABBBBBBBBBBBBBBBBB"
  secret_arn          = "arn:aws:secretsmanager:us-east-1:123456789012:secret:provider-abcdef"
  child_boundary_arn  = "arn:aws:iam::123456789012:policy/adp-dev-spp-01234567890123456789012345678901-child-boundary"
  managed_policy_arns = [for suffix in ["network", "identity", "lifecycle", "state-validation"] : "arn:aws:iam::123456789012:policy/adp-dev-spp-01234567890123456789012345678901-${suffix}"]
}

run "gateway_cannot_enroll_authority" {
  command = plan
  assert {
    condition     = toset(jsondecode(aws_iam_policy.gateway.policy).Statement[0].Action) == toset(["dynamodb:GetItem", "dynamodb:ConditionCheckItem"])
    error_message = "Gateway may only read/condition authority; enrollment remains protected-owner-only."
  }
  assert {
    condition     = toset(jsondecode(aws_iam_policy.gateway.policy).Statement[1].Action) == toset(["dynamodb:Query", "dynamodb:PutItem"])
    error_message = "Validation only appends immutable observations in the separate evidence table."
  }
  assert {
    condition     = toset(jsondecode(aws_dynamodb_resource_policy.authority.policy).Statement[1].Condition.StringNotLike["aws:userid"]) == toset([var.operator_role_id, "${var.operator_role_id}:*"])
    error_message = "A recreated operator must not inherit enrollment authority."
  }
  assert {
    condition     = aws_dynamodb_table.authority.deletion_protection_enabled && aws_dynamodb_table.evidence.deletion_protection_enabled
    error_message = "Authority and validation history must remain retained."
  }
  assert {
    condition     = toset(jsondecode(aws_iam_policy.gateway.policy).Statement[4].Resource) == setunion(var.managed_policy_arns, toset([var.child_boundary_arn]))
    error_message = "Policy read scope must name exact four shards and the independent child boundary."
  }
}

run "wrong_operator_identity_refused" {
  command = plan
  variables { operator_role_id = "AROACCCCCCCCCCCCCCCCC" }
  expect_failures = [terraform_data.owner]
}

run "extra_policy_identity_refused" {
  command = plan
  variables { managed_policy_arns = ["arn:aws:iam::aws:policy/AdministratorAccess"] }
  expect_failures = [terraform_data.owner]
}
