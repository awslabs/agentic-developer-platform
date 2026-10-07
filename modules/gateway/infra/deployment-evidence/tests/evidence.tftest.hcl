mock_provider "aws" {
  mock_resource "aws_iam_policy" { defaults = { arn = "arn:aws:iam::123456789012:policy/test" } }
  mock_resource "aws_s3_bucket" { defaults = { arn = "arn:aws:s3:::adp-test-deployment-evidence-123456789012" } }
}
variables {
  account_id              = "123456789012"
  environment             = "test"
  writer_roles            = ["adp-test-gateway-backend-trusted-deployment", "adp-test-frontend-trusted-deployment"]
  reader_role             = "adp-test-role-gateway-service"
  additional_reader_roles = ["adp-test-orchestration-tick-role"]
}
run "private_immutable_evidence" {
  command = apply
  assert {
    condition     = aws_s3_bucket.evidence.bucket == "adp-test-deployment-evidence-123456789012" && aws_s3_bucket.evidence.force_destroy != true
    error_message = "Dedicated bucket identity and deletion protection required."
  }
  assert {
    condition     = aws_s3_bucket_public_access_block.evidence.block_public_acls && aws_s3_bucket_public_access_block.evidence.block_public_policy && aws_s3_bucket_public_access_block.evidence.ignore_public_acls && aws_s3_bucket_public_access_block.evidence.restrict_public_buckets
    error_message = "Evidence must remain private."
  }
  assert {
    condition     = aws_s3_bucket_versioning.evidence.versioning_configuration[0].status == "Enabled"
    error_message = "Version identities must be retained."
  }
  assert {
    condition     = one(aws_s3_bucket_lifecycle_configuration.evidence.rule).expiration[0].days == 30
    error_message = "Preserve existing retention contract."
  }
  assert {
    condition     = jsondecode(aws_iam_policy.write.policy).Statement[0].Action == "s3:PutObject" && jsondecode(aws_iam_policy.write.policy).Statement[0].Condition.StringEquals["s3:if-none-match"] == "*"
    error_message = "Writer must have create-only evidence authority."
  }
  assert {
    condition     = length(aws_iam_role_policy_attachment.write) == 2 && aws_iam_role_policy_attachment.read.role == "adp-test-role-gateway-service"
    error_message = "Only selected deployment writers and Gateway reader may be attached."
  }
  assert {
    condition     = length(aws_iam_role_policy_attachment.additional_read) == 1 && aws_iam_role_policy_attachment.additional_read["adp-test-orchestration-tick-role"].policy_arn == aws_iam_policy.read.arn
    error_message = "The separately authenticated tick must receive the same scoped evidence reader policy."
  }
}
