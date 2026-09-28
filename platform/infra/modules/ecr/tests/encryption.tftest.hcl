mock_provider "aws" {}

variables {
  environment  = "dev"
  name_prefix  = "adp-dev"
  repositories = ["old-kms", "old-aes", "new-repository"]
  repository_encryption = {
    old-kms = {
      encryption_type = "KMS"
      kms_key         = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
    }
    old-aes = {
      encryption_type = "AES256"
    }
  }
}

override_resource {
  target          = aws_kms_key.ecr
  override_during = plan
  values = {
    arn = "arn:aws:kms:us-east-1:123456789012:key/22222222-2222-2222-2222-222222222222"
  }
}

run "preserve_existing_and_encrypt_new_repositories" {
  command = plan

  assert {
    condition     = aws_ecr_repository.main["old-kms"].encryption_configuration[0].kms_key == var.repository_encryption["old-kms"].kms_key
    error_message = "An existing KMS-encrypted repository must retain its immutable key."
  }
  assert {
    condition     = aws_ecr_repository.main["old-aes"].encryption_configuration[0].encryption_type == "AES256"
    error_message = "An existing AES256 repository must retain its encryption type."
  }
  assert {
    condition     = aws_ecr_repository.main["new-repository"].encryption_configuration[0].encryption_type == "KMS" && aws_ecr_repository.main["new-repository"].encryption_configuration[0].kms_key == aws_kms_key.ecr.arn
    error_message = "New repositories must use the dedicated managed ECR key."
  }
}

run "reject_missing_kms_key" {
  command = plan
  variables {
    repository_encryption = {
      old-kms = { encryption_type = "KMS" }
    }
  }
  expect_failures = [var.repository_encryption]
}
