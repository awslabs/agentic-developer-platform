# =============================================================================
# GitHub Auth Broker Lambda Module (Issue #520, Issue #525)
# =============================================================================
# Creates a Lambda function behind API Gateway that acts as a broker between
# GitHub OAuth and Cognito. The broker is only invokable via the REST API
# (no public Function URL). Routes: /api/auth/github/{start,callback}.
# =============================================================================

data "aws_caller_identity" "current" {}

locals {
  function_name = "${var.name_prefix}-github-auth-broker"
}

# --- IAM Role for the Lambda ---

resource "aws_iam_role" "broker" {
  name = "${local.function_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${local.function_name}-role"
    Service = "iam"
    Purpose = "github-auth-broker"
  })
}

# CloudWatch Logs
resource "aws_iam_role_policy" "broker_logs" {
  name = "${local.function_name}-logs"
  role = aws_iam_role.broker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.function_name}:*"
      }
    ]
  })
}

# Secrets Manager read (GitHub OAuth creds + org token)
resource "aws_iam_role_policy" "broker_secrets" {
  name = "${local.function_name}-secrets"
  role = aws_iam_role.broker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = compact([
          var.github_oauth_secret_arn,
          var.github_token_secret_arn
        ])
      }
    ]
  })
}

# Cognito admin operations (create user, set password, initiate auth)
resource "aws_iam_role_policy" "broker_cognito" {
  name = "${local.function_name}-cognito"
  role = aws_iam_role.broker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "cognito-idp:AdminCreateUser",
          "cognito-idp:AdminGetUser",
          "cognito-idp:AdminSetUserPassword",
          "cognito-idp:AdminInitiateAuth",
          "cognito-idp:AdminUpdateUserAttributes"
        ]
        Resource = var.cognito_user_pool_arn
      }
    ]
  })
}

# --- Lambda Function ---

resource "aws_lambda_function" "broker" {
  function_name = local.function_name
  role          = aws_iam_role.broker.arn
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 128

  # Placeholder — deployed via CI/CD after initial terraform apply
  filename         = data.archive_file.placeholder.output_path
  source_code_hash = data.archive_file.placeholder.output_base64sha256

  environment {
    variables = {
      GITHUB_CLIENT_ID         = "" # Set after deploy from Secrets Manager
      GITHUB_CLIENT_SECRET_ARN = var.github_oauth_secret_arn
      COGNITO_USER_POOL_ID     = var.cognito_user_pool_id
      COGNITO_CLIENT_ID        = var.cognito_client_id
      CALLBACK_URL             = "" # Updated after Function URL is created
      FRONTEND_URL             = var.frontend_url
      ALLOWLIST_MODE           = var.allowlist_mode
      ALLOWED_ORGS             = var.allowed_orgs
      GITHUB_TOKEN_SECRET_ARN  = var.github_token_secret_arn
      LOG_LEVEL                = "INFO"
    }
  }

  tags = merge(var.common_tags, {
    Name    = local.function_name
    Service = "lambda"
    Purpose = "github-auth-broker"
  })

  lifecycle {
    ignore_changes = [
      filename,
      source_code_hash,
      environment[0].variables["GITHUB_CLIENT_ID"],
      environment[0].variables["CALLBACK_URL"],
    ]
  }
}

# Placeholder zip for initial deploy (handler returns 503)
data "archive_file" "placeholder" {
  type        = "zip"
  output_path = "${path.module}/placeholder.zip"

  source {
    content  = <<-PY
      def handler(event, context):
          return {"statusCode": 503, "body": "Not deployed yet"}
    PY
    filename = "handler.py"
  }
}

# --- API Gateway Integration (Issue #525) ---
# Route: /api/auth/github/{proxy+} → Lambda (AWS_PROXY)
# Replaces the previously-deleted public Function URL.

resource "aws_api_gateway_resource" "auth" {
  rest_api_id = var.rest_api_id
  parent_id   = var.root_resource_id
  path_part   = "auth"
}

resource "aws_api_gateway_resource" "auth_github" {
  rest_api_id = var.rest_api_id
  parent_id   = aws_api_gateway_resource.auth.id
  path_part   = "github"
}

resource "aws_api_gateway_resource" "auth_github_proxy" {
  rest_api_id = var.rest_api_id
  parent_id   = aws_api_gateway_resource.auth_github.id
  path_part   = "{proxy+}"
}

resource "aws_api_gateway_method" "auth_github_proxy_any" {
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.auth_github_proxy.id
  http_method   = "ANY"
  authorization = "NONE"

  request_parameters = {
    "method.request.path.proxy" = true
  }
}

resource "aws_api_gateway_integration" "auth_github_proxy" {
  rest_api_id             = var.rest_api_id
  resource_id             = aws_api_gateway_resource.auth_github_proxy.id
  http_method             = aws_api_gateway_method.auth_github_proxy_any.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.broker.invoke_arn
}

# Permission for API Gateway to invoke the broker Lambda
resource "aws_lambda_permission" "api_gateway_invoke_broker" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.broker.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${var.rest_api_execution_arn}/*/*"
}

# --- CloudWatch Log Group ---

resource "aws_cloudwatch_log_group" "broker" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 30

  tags = merge(var.common_tags, {
    Name    = "${local.function_name}-logs"
    Service = "cloudwatch"
  })
}
