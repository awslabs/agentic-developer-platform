data "aws_caller_identity" "current" {}

data "archive_file" "ingest" {
  type        = "zip"
  source_dir  = var.ingest_source_dir
  output_path = "${path.module}/.build/ingest.zip"
}

resource "aws_lambda_function" "ingest" {
  function_name    = "${var.name_prefix}-gateway-ingest"
  description      = "Agent gateway ingest — channel adapters, session management, SQS enqueue"
  role             = aws_iam_role.ingest.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  memory_size      = 256
  timeout          = 30
  filename         = data.archive_file.ingest.output_path
  source_code_hash = data.archive_file.ingest.output_base64sha256

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      INPUT_QUEUE_URL     = var.input_queue_url
      RESPONSE_QUEUE_URL  = var.response_queue_url
      SESSIONS_TABLE_NAME = var.sessions_table_name
      AWS_REGION_NAME     = var.aws_region
      CLASSIFIER_MODEL    = var.classifier_model
      # GH App secret prefix for the github_actions dispatch path (ARC runner
      # path). The ingest Lambda uses the "ops" persona specifically — it has
      # write perms for issues/labels on the target repo. Secrets are stored at
      #   adp/<github_org>/gh-app-ops-{id,key}
      # by the ARC setup process (see SETUP-GUIDE.md).
      # The code appends `-id` and `-key` at runtime.
      GH_APP_SECRET_PREFIX = "adp/${var.github_org}/gh-app-ops"
      ARTIFACTS_BUCKET     = var.artifacts_bucket_name
      ARTIFACTS_TABLE      = var.artifacts_table_name
      # Stage C (#186): WebSocket post-back endpoint for upload-token /
      # upload-complete responses. API Gateway WebSocket discards synchronous
      # Lambda returns; the ingest Lambda must push responses back via
      # apigatewaymanagementapi.post_to_connection, which needs this URL.
      WS_API_ENDPOINT = var.ws_api_endpoint
    }
  }

  tags = var.tags
}

resource "aws_iam_role" "ingest" {
  name = "${var.name_prefix}-gateway-ingest"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ingest_basic" {
  role       = aws_iam_role.ingest.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "ingest_sqs" {
  name = "sqs-send"
  role = aws_iam_role.ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
        Resource = var.input_queue_arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = var.response_queue_arn
      },
    ]
  })
}

resource "aws_iam_role_policy" "ingest_bedrock" {
  name = "bedrock"
  role = aws_iam_role.ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["bedrock:InvokeModel"]
      Resource = ["arn:aws:bedrock:*::foundation-model/*", "arn:aws:bedrock:*:*:inference-profile/*"]
    }]
  })
}

resource "aws_iam_role_policy" "ingest_dynamodb" {
  name = "dynamodb"
  role = aws_iam_role.ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      # DeleteItem is required for $disconnect cleanup — without it the
      # handler logs "AccessDeniedException on dynamodb:DeleteItem" and
      # leaves stale connection rows that confuse response routing.
      Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:DeleteItem"]
      Resource = [
        var.sessions_table_arn, "${var.sessions_table_arn}/index/*",
        var.artifacts_table_arn, "${var.artifacts_table_arn}/index/*",
      ]
    }]
  })
}

# S3 presigned URL support for upload-token / upload-complete actions.
# generate_presigned_url("put_object") signs with the Lambda's credentials,
# so the role needs s3:PutObject. s3:GetObject is included for future
# download-token support.
resource "aws_iam_role_policy" "ingest_s3_uploads" {
  name = "s3-uploads"
  role = aws_iam_role.ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:GetObject"]
      Resource = "${var.artifacts_bucket_arn}/*"
    }]
  })
}

# Stage C (#186): allow the ingest Lambda to post responses back to WebSocket
# clients for upload-token / upload-complete requests. API Gateway WebSocket
# discards synchronous Lambda returns; async post-back is the only delivery
# path. Scoped to the WS API execution ARN only — not account-wide.
resource "aws_iam_role_policy" "ingest_apigw_manage_connections" {
  count = var.enable_ws_policies ? 1 : 0
  name  = "apigw-manage-connections"
  role  = aws_iam_role.ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["execute-api:ManageConnections"]
      Resource = "${var.ws_execution_arn}/*"
    }]
  })
}

# GH App secrets for the github_actions dispatch path (ARC runner path).
# Wildcard-scoped to the configured org's namespace so the policy stays valid
# as new personas are added manually (see SETUP-GUIDE.md) without Terraform
# changes. The org must match what was used to write the secrets.
resource "aws_iam_role_policy" "ingest_gh_app_secrets" {
  name = "gh-app-secrets-read"
  role = aws_iam_role.ingest.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:adp/${var.github_org}/gh-app-*"
    }]
  })
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${var.name_prefix}-gateway-ingest"
  retention_in_days = 30
  kms_key_id        = var.cloudwatch_kms_key_arn
  tags              = var.tags
}

# --- Response Lambda ---

data "archive_file" "response" {
  type        = "zip"
  source_dir  = var.response_source_dir
  output_path = "${path.module}/.build/response.zip"
}

resource "aws_lambda_function" "response" {
  function_name    = "${var.name_prefix}-gateway-response"
  description      = "Agent gateway response — channel routers, session serialization"
  role             = aws_iam_role.response.arn
  handler          = "handler.lambda_handler"
  runtime          = "python3.12"
  memory_size      = 256
  timeout          = 30
  filename         = data.archive_file.response.output_path
  source_code_hash = data.archive_file.response.output_base64sha256

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      INPUT_QUEUE_URL     = var.input_queue_url
      SESSIONS_TABLE_NAME = var.sessions_table_name
      WS_API_ENDPOINT     = var.ws_api_endpoint
      WS_API_ID           = var.ws_api_id
      ENVIRONMENT         = var.environment
      AWS_REGION_NAME     = var.aws_region
    }
  }

  tags = var.tags
}

resource "aws_lambda_event_source_mapping" "response_sqs" {
  event_source_arn = var.response_queue_arn
  function_name    = aws_lambda_function.response.arn
  # Issue #164: batch_size=10 reduces per-message polling overhead. The handler
  # already iterates `event["Records"]` so multi-record batches work as-is.
  # For FIFO, all records in a batch share the same MessageGroupId, so
  # in-session ordering is preserved within the batch.
  batch_size = 10
  enabled    = true

  # Issue #164: Allow up to 50 concurrent Lambda invocations for the response
  # queue. For FIFO queues, Lambda defaults to 5 concurrent pollers and scales
  # +5/min — far too slow when many sessions are active (E2E tests, multi-user).
  # With scaling_config, Lambda can invoke up to this many functions concurrently
  # across different MessageGroupIds (sessions), eliminating cross-session
  # head-of-line blocking. Within a single MessageGroupId, ordering is still
  # preserved (only one invocation per group at a time).
  scaling_config {
    maximum_concurrency = 50
  }
}

resource "aws_iam_role" "response" {
  name = "${var.name_prefix}-gateway-response"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "response_basic" {
  role       = aws_iam_role.response.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "response_sqs" {
  name = "sqs-access"
  role = aws_iam_role.response.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = var.response_queue_arn
      },
      {
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = var.input_queue_arn
      },
    ]
  })
}

resource "aws_iam_role_policy" "response_dynamodb" {
  name = "dynamodb"
  role = aws_iam_role.response.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      # DeleteItem is required to evict stale connection rows the router
      # discovers while trying to deliver a response (GoneException on
      # post_to_connection). Without it the dead row stays and repeats the
      # same failure on every subsequent delivery attempt.
      Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:DeleteItem"]
      Resource = [var.sessions_table_arn, "${var.sessions_table_arn}/index/*"]
    }]
  })
}

resource "aws_iam_role_policy" "response_apigw" {
  # Only create when a WS API has been wired through — otherwise the resource ARN
  # is empty and AWS rejects the policy as malformed.
  count = var.enable_ws_policies ? 1 : 0
  name  = "apigw"
  role  = aws_iam_role.response.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["execute-api:ManageConnections"]
      Resource = "${var.ws_execution_arn}/*"
    }]
  })
}

resource "aws_iam_role_policy" "response_secrets" {
  name = "secrets"
  role = aws_iam_role.response.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = "arn:aws:secretsmanager:${var.aws_region}:*:secret:adp/*"
    }]
  })
}

resource "aws_cloudwatch_log_group" "response" {
  name              = "/aws/lambda/${var.name_prefix}-gateway-response"
  retention_in_days = 30
  kms_key_id        = var.cloudwatch_kms_key_arn
  tags              = var.tags
}
