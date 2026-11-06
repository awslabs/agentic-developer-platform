mock_provider "aws" {
  mock_resource "aws_kms_key" {
    override_during = plan
    defaults        = { arn = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111" }
  }
}
variables {
  environment      = "dev"
  account_id       = "123456789012"
  gateway_role_arn = "arn:aws:iam::123456789012:role/adp-dev-role-gateway-service"
  issuer_origin    = "https://gateway.example.test"
  approval_origin  = "https://gateway.example.test"
}
run "default_disabled" {
  command = plan
  assert {
    condition     = length(aws_kms_key.issuer) == 0 && jsondecode(aws_ssm_parameter.configuration.value).enabled == false
    error_message = "Default deployment must create neither a signer nor native credential authority."
  }
}
run "scoped_signer" {
  command = plan
  variables { enabled = true }
  assert {
    condition     = aws_kms_key.issuer[0].key_usage == "SIGN_VERIFY" && aws_kms_key.issuer[0].customer_master_key_spec == "RSA_3072" && aws_kms_key.issuer[0].deletion_window_in_days == 30
    error_message = "Use a dedicated asymmetric signing key with delayed deletion."
  }
  assert {
    condition     = jsondecode(aws_kms_key.issuer[0].policy).Statement[2].Principal.AWS == var.gateway_role_arn && jsondecode(aws_kms_key.issuer[0].policy).Statement[2].Condition.StringEquals["kms:SigningAlgorithm"] == "RSASSA_PSS_SHA_256" && jsondecode(aws_kms_key.issuer[0].policy).Statement[3].Effect == "Deny" && jsondecode(aws_kms_key.issuer[0].policy).Statement[3].Condition.ArnNotEquals["aws:PrincipalArn"] == var.gateway_role_arn
    error_message = "Only the exact Gateway role may sign the native wire algorithm."
  }
  assert {
    condition     = jsondecode(aws_kms_key.issuer[0].policy).Statement[4].Effect == "Deny" && jsondecode(aws_kms_key.issuer[0].policy).Statement[4].Condition.StringNotEquals["kms:SigningAlgorithm"] == "RSASSA_PSS_SHA_256"
    error_message = "An independently granted IAM permission must not authorize a different signing algorithm."
  }
  assert {
    condition     = jsondecode(aws_ssm_parameter.configuration.value).key_arn == aws_kms_key.issuer[0].arn && aws_ssm_parameter.configuration.name == "/adp/dev/gateway/native-credentials-config"
    error_message = "Publish the generated immutable key, never a guessed key or a secret."
  }
}
run "wrong_signer" {
  command = plan
  variables {
    gateway_role_arn = "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"
  }
  expect_failures = [var.gateway_role_arn]
}
run "credential_origin" {
  command = plan
  variables { issuer_origin = "https://owner@gateway.example.test" }
  expect_failures = [var.issuer_origin]
}
run "path_origin" {
  command = plan
  variables { approval_origin = "https://gateway.example.test/api" }
  expect_failures = [var.approval_origin]
}
