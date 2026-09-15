# Mocked applies exercise the delivery wiring and disable transition without
# AWS credentials, infrastructure changes or paid model invocations.
mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  mock_data "aws_region" {
    defaults = { name = "us-east-1" }
  }
  mock_data "aws_partition" {
    defaults = { partition = "aws", dns_suffix = "amazonaws.com" }
  }
  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111"
      key_id = "11111111-1111-1111-1111-111111111111"
    }
  }
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/adp-test-bedrock-logging-us-east-1" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = {
      id  = "adp-test-bedrock-logs-123456789012-us-east-1"
      arn = "arn:aws:s3:::adp-test-bedrock-logs-123456789012-us-east-1"
    }
  }
}

variables {
  name_prefix = "adp-test"
}

run "default_delivery_includes_large_payloads" {
  command = apply

  assert {
    condition = (
      length(aws_bedrock_model_invocation_logging_configuration.this) == 1 &&
      aws_bedrock_model_invocation_logging_configuration.this[0].logging_config.text_data_delivery_enabled &&
      aws_bedrock_model_invocation_logging_configuration.this[0].logging_config.image_data_delivery_enabled &&
      aws_bedrock_model_invocation_logging_configuration.this[0].logging_config.embedding_data_delivery_enabled &&
      aws_bedrock_model_invocation_logging_configuration.this[0].logging_config.video_data_delivery_enabled
    )
    error_message = "Fresh deployments must log all four provider-supported modalities by default."
  }

  assert {
    condition = (
      aws_bedrock_model_invocation_logging_configuration.this[0].logging_config.s3_config.bucket_name == aws_s3_bucket.logs.id &&
      aws_bedrock_model_invocation_logging_configuration.this[0].logging_config.cloudwatch_config.log_group_name == aws_cloudwatch_log_group.invocations.name &&
      aws_bedrock_model_invocation_logging_configuration.this[0].logging_config.cloudwatch_config.role_arn == aws_iam_role.delivery.arn &&
      aws_bedrock_model_invocation_logging_configuration.this[0].logging_config.cloudwatch_config.large_data_delivery_s3_config.bucket_name == aws_s3_bucket.logs.id &&
      aws_bedrock_model_invocation_logging_configuration.this[0].logging_config.cloudwatch_config.large_data_delivery_s3_config.key_prefix == "large-data"
    )
    error_message = "Both destinations and S3 overflow must be wired; large prompts must not lose their bodies."
  }

  assert {
    condition = (
      aws_cloudwatch_log_group.invocations.retention_in_days == 30 &&
      one(aws_s3_bucket_lifecycle_configuration.logs.rule).expiration[0].days == 30 &&
      aws_cloudwatch_log_group.invocations.kms_key_id == aws_kms_key.logs.arn &&
      one(aws_s3_bucket_server_side_encryption_configuration.logs.rule).apply_server_side_encryption_by_default[0].kms_master_key_id == aws_kms_key.logs.arn &&
      aws_kms_key.logs.enable_key_rotation && !aws_s3_bucket.logs.force_destroy
    )
    error_message = "Logs must be encrypted, expire after 30 days, and resist accidental bucket purging."
  }

  assert {
    condition = (
      aws_s3_bucket_public_access_block.logs.block_public_acls &&
      aws_s3_bucket_public_access_block.logs.block_public_policy &&
      aws_s3_bucket_public_access_block.logs.ignore_public_acls &&
      aws_s3_bucket_public_access_block.logs.restrict_public_buckets &&
      one(aws_s3_bucket_ownership_controls.logs.rule).object_ownership == "BucketOwnerEnforced"
    )
    error_message = "Invocation payloads must use a private bucket with ACLs disabled."
  }
}

run "delivery_permissions_match_both_prefixes_and_source" {
  command = apply

  assert {
    condition = (
      jsondecode(aws_s3_bucket_policy.logs.policy).Statement[0].Action == "s3:PutObject" &&
      jsondecode(aws_s3_bucket_policy.logs.policy).Statement[0].Principal.Service == "bedrock.amazonaws.com" &&
      jsondecode(aws_s3_bucket_policy.logs.policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012" &&
      jsondecode(aws_s3_bucket_policy.logs.policy).Statement[0].Condition.ArnLike["aws:SourceArn"] == "arn:aws:bedrock:us-east-1:123456789012:*" &&
      toset(jsondecode(aws_s3_bucket_policy.logs.policy).Statement[0].Resource) == toset([
        "${aws_s3_bucket.logs.arn}/invocations/AWSLogs/123456789012/BedrockModelInvocationLogs/*",
        "${aws_s3_bucket.logs.arn}/large-data/AWSLogs/123456789012/BedrockModelInvocationLogs/*",
      ]) &&
      jsondecode(aws_s3_bucket_policy.logs.policy).Statement[1].Effect == "Deny" &&
      jsondecode(aws_s3_bucket_policy.logs.policy).Statement[1].Condition.Bool["aws:SecureTransport"] == "false"
    )
    error_message = "Allow only this account/region's Bedrock service to write both delivery prefixes, and require TLS."
  }

  assert {
    condition = (
      jsondecode(aws_iam_role.delivery.assume_role_policy).Statement[0].Principal.Service == "bedrock.amazonaws.com" &&
      jsondecode(aws_iam_role.delivery.assume_role_policy).Statement[0].Condition.StringEquals["aws:SourceAccount"] == "123456789012" &&
      jsondecode(aws_iam_role.delivery.assume_role_policy).Statement[0].Condition.ArnLike["aws:SourceArn"] == "arn:aws:bedrock:us-east-1:123456789012:*" &&
      toset(jsondecode(aws_iam_role_policy.delivery.policy).Statement[0].Action) == toset(["logs:CreateLogStream", "logs:PutLogEvents"]) &&
      jsondecode(aws_iam_role_policy.delivery.policy).Statement[0].Resource == "arn:aws:logs:us-east-1:123456789012:log-group:/aws/bedrock/adp-test/model-invocations:log-stream:aws/bedrock/modelinvocations"
    )
    error_message = "The Bedrock delivery role must be source-scoped and limited to its invocation log stream."
  }

  assert {
    condition = (
      jsondecode(aws_kms_key.logs.policy).Statement[1].Principal.Service == "bedrock.amazonaws.com" &&
      jsondecode(aws_kms_key.logs.policy).Statement[1].Action == "kms:GenerateDataKey" &&
      jsondecode(aws_kms_key.logs.policy).Statement[1].Condition.StringEquals["aws:SourceAccount"] == "123456789012" &&
      jsondecode(aws_kms_key.logs.policy).Statement[1].Condition.ArnLike["aws:SourceArn"] == "arn:aws:bedrock:us-east-1:123456789012:*" &&
      jsondecode(aws_kms_key.logs.policy).Statement[2].Condition.ArnEquals["kms:EncryptionContext:aws:logs:arn"] == "arn:aws:logs:us-east-1:123456789012:log-group:/aws/bedrock/adp-test/model-invocations"
    )
    error_message = "Encrypted delivery must authorize Bedrock and CloudWatch for the intended source and log group."
  }
}

run "disable_keeps_existing_log_destinations" {
  command = apply
  variables {
    enabled = false
  }
  assert {
    condition = (
      length(aws_bedrock_model_invocation_logging_configuration.this) == 0 &&
      output.bucket_name == run.default_delivery_includes_large_payloads.bucket_name &&
      output.log_group_name == run.default_delivery_includes_large_payloads.log_group_name &&
      output.key_arn == run.default_delivery_includes_large_payloads.key_arn
    )
    error_message = "Disabling logging must remove only the singleton configuration, retaining stored logs and their encryption key."
  }
}

run "retention_is_configurable" {
  command = plan
  variables {
    retention_in_days = 7
  }
  assert {
    condition = (
      aws_cloudwatch_log_group.invocations.retention_in_days == 7 &&
      one(aws_s3_bucket_lifecycle_configuration.logs.rule).expiration[0].days == 7
    )
    error_message = "CloudWatch and S3 must share the selected retention."
  }
}

run "unbounded_retention_is_rejected" {
  command = plan
  variables {
    retention_in_days = 0
  }
  expect_failures = [var.retention_in_days]
}
