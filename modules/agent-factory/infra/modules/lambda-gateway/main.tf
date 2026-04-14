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
  environment {
    variables = {
      INPUT_QUEUE_URL     = var.input_queue_url
      SESSIONS_TABLE_NAME = var.sessions_table_name
      AWS_REGION_NAME     = var.aws_region
    }
  }
  tags = var.tags
}

resource "aws_iam_role" "ingest" {
  name               = "${var.name_prefix}-gateway-ingest"
  assume_role_policy = jsonencode({ Version = "2012-10-17"; Statement = [{ Action = "sts:AssumeRole"; Effect = "Allow"; Principal = { Service = "lambda.amazonaws.com" } }] })
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "ingest_basic" {
  role       = aws_iam_role.ingest.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "ingest_sqs" {
  name   = "sqs-send"
  role   = aws_iam_role.ingest.id
  policy = jsonencode({ Version = "2012-10-17"; Statement = [{ Effect = "Allow"; Action = ["sqs:SendMessage", "sqs:GetQueueAttributes"]; Resource = var.input_queue_arn }] })
}

resource "aws_iam_role_policy" "ingest_dynamodb" {
  name   = "dynamodb"
  role   = aws_iam_role.ingest.id
  policy = jsonencode({ Version = "2012-10-17"; Statement = [{ Effect = "Allow"; Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]; Resource = [var.sessions_table_arn, "${var.sessions_table_arn}/index/*"] }] })
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${var.name_prefix}-gateway-ingest"
  retention_in_days = 30
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
  batch_size       = 1
  enabled          = true
}

resource "aws_iam_role" "response" {
  name               = "${var.name_prefix}-gateway-response"
  assume_role_policy = jsonencode({ Version = "2012-10-17"; Statement = [{ Action = "sts:AssumeRole"; Effect = "Allow"; Principal = { Service = "lambda.amazonaws.com" } }] })
  tags               = var.tags
}

resource "aws_iam_role_policy_attachment" "response_basic" {
  role       = aws_iam_role.response.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "response_sqs" {
  name   = "sqs-access"
  role   = aws_iam_role.response.id
  policy = jsonencode({ Version = "2012-10-17"; Statement = [{ Effect = "Allow"; Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]; Resource = var.response_queue_arn }, { Effect = "Allow"; Action = ["sqs:SendMessage"]; Resource = var.input_queue_arn }] })
}

resource "aws_iam_role_policy" "response_dynamodb" {
  name   = "dynamodb"
  role   = aws_iam_role.response.id
  policy = jsonencode({ Version = "2012-10-17"; Statement = [{ Effect = "Allow"; Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]; Resource = [var.sessions_table_arn, "${var.sessions_table_arn}/index/*"] }] })
}

resource "aws_iam_role_policy" "response_apigw" {
  name   = "apigw"
  role   = aws_iam_role.response.id
  policy = jsonencode({ Version = "2012-10-17"; Statement = [{ Effect = "Allow"; Action = ["execute-api:ManageConnections"]; Resource = "${var.ws_execution_arn}/*" }] })
}

resource "aws_iam_role_policy" "response_secrets" {
  name   = "secrets"
  role   = aws_iam_role.response.id
  policy = jsonencode({ Version = "2012-10-17"; Statement = [{ Effect = "Allow"; Action = ["secretsmanager:GetSecretValue"]; Resource = "arn:aws:secretsmanager:${var.aws_region}:*:secret:adp/*" }] })
}

resource "aws_cloudwatch_log_group" "response" {
  name              = "/aws/lambda/${var.name_prefix}-gateway-response"
  retention_in_days = 30
  tags              = var.tags
}
