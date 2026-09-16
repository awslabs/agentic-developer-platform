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

# --- Exchange-Code Table (Issue #4133) ---
# Holds the pending Cognito session between the GitHub callback redirect and the
# SPA's POST /exchange, so session tokens never travel in a URL. Rows are
# single-use (deleted on redemption) and short-lived; TTL only sweeps codes that
# were never redeemed (abandoned logins).

resource "aws_dynamodb_table" "auth_codes" {
  name         = "${local.function_name}-codes"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "code"

  attribute {
    name = "code"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # Bearer tokens at rest — encrypt with the customer-managed key.
  server_side_encryption {
    enabled     = true
    kms_key_arn = var.dynamodb_kms_key_arn
  }

  # No point_in_time_recovery: rows live ~2 minutes and are worthless once
  # redeemed. Backing up short-lived bearer tokens would add exposure, not value.

  tags = merge(var.common_tags, {
    Name    = "${local.function_name}-codes"
    Service = "dynamodb"
    Purpose = "github-auth-session-handoff"
  })
}

resource "aws_iam_role_policy" "broker_auth_codes" {
  name = "${local.function_name}-auth-codes"
  role = aws_iam_role.broker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "AuthCodeReadWrite"
        Effect = "Allow"
        # No GetItem: the exchange consumes codes via delete_item(ALL_OLD) so the
        # read and the invalidation are one atomic call (no replay window).
        Action = [
          "dynamodb:PutItem",
          "dynamodb:DeleteItem"
        ]
        Resource = [aws_dynamodb_table.auth_codes.arn]
      }
      ], var.dynamodb_kms_key_arn != "" ? [
      {
        Sid    = "AuthCodeKMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [var.dynamodb_kms_key_arn]
      }
      ] : [],
      # Issue #4849: read the membership-eligibility projection (member_org_ids on
      # the identity-index rows). GetItem only — the gateway API is the sole writer.
      # ARNs arrive as a variable, not a cross-module reference: the tables live in
      # the gateway root module, and this module is already ON the documented
      # cloudfront -> api_gateway -> github_auth_broker -> cloudfront dependency
      # loop (modules/gateway/infra/main.tf:723-736), so reaching back into the
      # root from here is exactly what closes it.
      # Conditional because an empty Resource list is a malformed policy, not an
      # empty grant.
      length(var.identity_index_table_arns) > 0 ? [
        {
          Sid      = "IdentityIndexProjectionRead"
          Effect   = "Allow"
          Action   = ["dynamodb:GetItem"]
          Resource = var.identity_index_table_arns
        }
    ] : [])
  })
}

# --- Lambda Function ---

resource "aws_lambda_function" "broker" {
  function_name                  = local.function_name
  role                           = aws_iam_role.broker.arn
  handler                        = "handler.handler"
  runtime                        = "python3.12"
  timeout                        = 30
  memory_size                    = 128
  reserved_concurrent_executions = var.enable_reserved_concurrency ? 10 : -1

  tracing_config {
    mode = "Active"
  }

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
      ALLOW_OPEN_SIGNUP        = var.allow_open_signup ? "true" : "false"
      GITHUB_TOKEN_SECRET_ARN  = var.github_token_secret_arn
      AUTH_CODE_TABLE          = aws_dynamodb_table.auth_codes.name
      LOG_LEVEL                = "INFO"

      # Issue #4849: membership-eligibility projection tables (shadow-mode read).
      # Env var and code ship together by construction here — this apply sets the
      # vars and github-auth-broker-deploy.yml ships the code that reads them; the
      # read is inert (log-only) so the ordering cannot cause a login outage the
      # way the ALLOWLIST_MODE / ALLOW_OPEN_SIGNUP split did (CLAUDE.md).
      IDENTITY_INDEX_TABLE        = var.identity_index_table_name
      USER_IDENTITY_INDEX_TABLE   = var.user_identity_index_table_name
      USER_IDENTITY_INDEX_V2_READ = var.user_identity_index_v2_read
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

# --- API Gateway Integration (Issue #525, Issue #1011) ---
# The /auth/github/{proxy+} route is now defined in the api-gateway module's
# OpenAPI body (Issue #1011). Previously, imperative aws_api_gateway_resource
# blocks here were overwritten by the body attribute on each apply.
# The Lambda permission is also managed by the api-gateway module.

# --- CloudWatch Log Group ---

resource "aws_cloudwatch_log_group" "broker" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = 30
  kms_key_id        = var.cloudwatch_kms_key_arn

  tags = merge(var.common_tags, {
    Name    = "${local.function_name}-logs"
    Service = "cloudwatch"
  })
}
