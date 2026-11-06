mock_provider "aws" {}

variables {
  account_id        = "123456789012"
  region            = "us-east-1"
  environment       = "test"
  gateway_role_name = "adp-test-gateway"
}

run "private_versioned_source_storage" {
  command = apply
  assert {
    condition     = aws_s3_bucket_public_access_block.snapshots.block_public_acls && aws_s3_bucket_public_access_block.snapshots.block_public_policy && aws_s3_bucket_public_access_block.snapshots.ignore_public_acls && aws_s3_bucket_public_access_block.snapshots.restrict_public_buckets
    error_message = "Source snapshots must not be public."
  }
  assert {
    condition     = aws_s3_bucket_versioning.snapshots.versioning_configuration[0].status == "Enabled"
    error_message = "Snapshot receipts require immutable S3 object versions."
  }
  assert {
    condition     = aws_iam_role_policy.gateway.role == var.gateway_role_name && !contains(jsondecode(aws_iam_role_policy.gateway.policy).Statement[0].Action, "s3:DeleteObject")
    error_message = "Grant only the selected gateway capture/read access and preserve history."
  }
}
