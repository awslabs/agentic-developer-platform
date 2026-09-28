# #5013: both Chat Lambdas need the sessions/artifacts key, which is distinct
# from the gateway identity-index key. Providers are mocked: no AWS calls.
mock_provider "aws" {
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock-lambda-role" }
  }
}
mock_provider "archive" {}

variables {
  name_prefix                = "adp-test-agent"
  environment                = "test"
  aws_region                 = "us-east-1"
  github_org                 = "test-org"
  ingest_source_dir          = "."
  response_source_dir        = "."
  input_queue_url            = "https://sqs.us-east-1.amazonaws.com/123456789012/tasks.fifo"
  input_queue_arn            = "arn:aws:sqs:us-east-1:123456789012:tasks.fifo"
  response_queue_url         = "https://sqs.us-east-1.amazonaws.com/123456789012/responses.fifo"
  response_queue_arn         = "arn:aws:sqs:us-east-1:123456789012:responses.fifo"
  sessions_table_name        = "sessions"
  sessions_table_arn         = "arn:aws:dynamodb:us-east-1:123456789012:table/sessions"
  artifacts_table_name       = "artifacts"
  artifacts_table_arn        = "arn:aws:dynamodb:us-east-1:123456789012:table/artifacts"
  artifacts_bucket_name      = "artifacts"
  artifacts_bucket_arn       = "arn:aws:s3:::artifacts"
  dynamodb_kms_key_arn       = "arn:aws:kms:us-east-1:123456789012:key/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
  identity_index_table_name  = "identity-index"
  identity_index_table_arn   = "arn:aws:dynamodb:us-east-1:123456789012:table/identity-index"
  identity_index_kms_key_arn = "arn:aws:kms:us-east-1:123456789012:key/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
}

run "chat_lambdas_use_the_table_key_only_via_dynamodb" {
  command = apply

  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }

  assert {
    condition = (
      aws_iam_role_policy.ingest_dynamodb_kms.role == aws_iam_role.ingest.id &&
      aws_iam_role_policy.response_dynamodb_kms.role == aws_iam_role.response.id
    )
    error_message = "Attach the KMS grant to both ingest and response execution roles."
  }

  assert {
    condition = alltrue([
      for policy in [aws_iam_role_policy.ingest_dynamodb_kms.policy, aws_iam_role_policy.response_dynamodb_kms.policy] :
      length(jsondecode(policy).Statement) == 1 &&
      jsondecode(policy).Statement[0].Effect == "Allow" &&
      jsondecode(policy).Statement[0].Resource == var.dynamodb_kms_key_arn &&
      jsondecode(policy).Statement[0].Resource != var.identity_index_kms_key_arn &&
      toset(jsondecode(policy).Statement[0].Action) == toset(["kms:Decrypt", "kms:DescribeKey"]) &&
      jsondecode(policy).Statement[0].Condition.StringEquals["kms:ViaService"] == "dynamodb.us-east-1.amazonaws.com" &&
      jsondecode(policy).Statement[0].Condition.StringEquals["kms:CallerAccount"] == "123456789012"
    ])
    error_message = "Allow only Decrypt/DescribeKey on the actual table key through this account's regional DynamoDB service."
  }
}

run "reject_missing_table_key" {
  command = plan
  variables {
    dynamodb_kms_key_arn = ""
  }
  expect_failures = [var.dynamodb_kms_key_arn]
}

run "reject_wildcard_table_key" {
  command = plan
  variables {
    dynamodb_kms_key_arn = "*"
  }
  expect_failures = [var.dynamodb_kms_key_arn]
}

run "factory_without_github_has_no_legacy_secret_access" {
  command = plan
  variables {
    github_org = ""
  }
  assert {
    condition     = length(aws_iam_role_policy.ingest_gh_app_secrets) == 0
    error_message = "Installing factory before GitHub setup must not grant a guessed organization's secrets."
  }
  assert {
    condition     = aws_lambda_function.ingest.environment[0].variables.GH_APP_SECRET_PREFIX == ""
    error_message = "Legacy GitHub dispatch must remain unconfigured until an organization is selected."
  }
}

run "configured_factory_retains_legacy_secret_namespace" {
  command = plan
  override_data {
    target = data.aws_caller_identity.current
    values = { account_id = "123456789012" }
  }
  assert {
    condition     = aws_lambda_function.ingest.environment[0].variables.GH_APP_SECRET_PREFIX == "adp/test-org/gh-app-ops"
    error_message = "Existing factory installations must retain their configured organization."
  }
  assert {
    condition     = jsondecode(aws_iam_role_policy.ingest_gh_app_secrets[0].policy).Statement[0].Resource == "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/test-org/gh-app-*"
    error_message = "Existing factory secret access must remain scoped to the configured organization."
  }
}
