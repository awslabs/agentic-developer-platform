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
    condition     = !contains(flatten([for s in output.grants : s.Action]), "kms:Decrypt")
    error_message = "Missing KMS configuration must not restore ambient decryption."
  }
  assert {
    condition     = !can(regex("security-agent/|security-scans-", jsonencode(output.grants)))
    error_message = "An ordinary runner must never read or alter the security ledger."
  }
}
run "kms_decrypt_absent_from_boundary_without_kms_arns" {
  command = plan
  variables {
    transport_secret_arns = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/example/gh-app-dev-key-Ab12Cd"]
  }
  assert {
    condition     = !can(regex("kms:Decrypt", jsonencode(output.boundary)))
    error_message = "Transport secrets without KMS ARNs must not add kms:Decrypt to the ceiling."
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

run "kms_alias_is_rejected" {
  command = plan
  variables {
    transport_secret_arns     = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/example/gh-app-dev-key-Ab12Cd"]
    transport_secret_kms_arns = ["arn:aws:kms:us-east-1:123456789012:alias/aws/secretsmanager"]
  }
  expect_failures = [var.transport_secret_kms_arns]
}
run "malformed_key_id_is_rejected" {
  command = plan
  variables {
    transport_secret_arns     = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/example/gh-app-dev-key-Ab12Cd"]
    transport_secret_kms_arns = ["arn:aws:kms:us-east-1:123456789012:key/------------------------------------"]
  }
  expect_failures = [var.transport_secret_kms_arns]
}
run "multi_region_key_remains_exact" {
  command = plan
  variables {
    transport_secret_arns     = ["arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/example/gh-app-dev-key-Ab12Cd"]
    transport_secret_kms_arns = ["arn:aws:kms:us-east-1:123456789012:key/mrk-11111111222233334444555555555555"]
  }
  assert {
    condition     = one([for statement in output.grants : statement if statement.Sid == "SecretDecryption"]).Resource == var.transport_secret_kms_arns
    error_message = "An exact AWS multi-region key ARN must remain scoped to that key."
  }
}
