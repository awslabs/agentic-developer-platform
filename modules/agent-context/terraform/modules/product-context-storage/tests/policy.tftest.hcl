mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_data "aws_region" {
    defaults = { name = "us-east-1" }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
}
variables {
  name_prefix       = "fixture"
  bucket_name       = "fixture-context"
  gateway_role_arn  = "arn:aws:iam::123456789012:role/fixture-gateway"
  gateway_role_name = "fixture-gateway"
}
run "bounded_sessions" {
  command = plan
  assert {
    condition     = jsondecode(aws_iam_role_policy.product_session.policy).Statement[0].Resource == "arn:aws:s3:::fixture-context/tenants/$${aws:PrincipalTag/adp_tenant_id}/products/$${aws:PrincipalTag/adp_product_id}/*"
    error_message = "Object permissions must bind both tenant and product session tags."
  }
  assert {
    condition     = jsondecode(aws_iam_role.product_session.assume_role_policy).Statement[0].Principal.AWS == var.gateway_role_arn
    error_message = "Only the authorized gateway may issue product sessions."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.product_session.policy).Statement[2].Condition.StringEquals["kms:ViaService"] == "s3.us-east-1.amazonaws.com"
    error_message = "KMS access must be usable only through S3."
  }
  assert {
    condition     = length(aws_iam_role_policy.gateway_direct_deny) == 0
    error_message = "The initial rollout must not deny existing Gateway traffic."
  }
}
run "dedicated_keys_and_cutover" {
  command = plan
  variables {
    managed_product_keys       = { example = { tenant_id = "tenant-a", product_id = "product-a" } }
    upload_origins             = ["https://app.example.com"]
    deny_gateway_direct_access = true
  }
  assert {
    condition     = aws_kms_key.product["example"].tags["adp:product_id"] == "product-a" && aws_kms_key.product["example"].tags["adp:tenant_id"] == "tenant-a"
    error_message = "Dedicated keys require the same ownership tags checked by the API."
  }
  assert {
    condition     = aws_kms_alias.product["example"].name == "alias/adp-context/${sha256("tenant-a/product-a")}"
    error_message = "API and Terraform must agree on dedicated-key discovery."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.gateway_direct_deny[0].policy).Statement[1].Effect == "Deny"
    error_message = "Root listing must not disclose product keys after cutover."
  }
}
run "reject_wildcard_cors" {
  command = plan
  variables { upload_origins = ["https://*.example.com"] }
  expect_failures = [var.upload_origins]
}
