# =============================================================================
# Pre Sign-Up Lambda Trigger (Issue #314)
# =============================================================================
#
# This Lambda function is triggered by Cognito before a user is created.
# It gates sign-up for external provider (GitHub) users based on:
# - org mode: Check GitHub org membership
# - explicit mode: Check DynamoDB allowlist
# - open mode: Allow all
#

# Package the Lambda code
# Issue #4849: multi-source archive so the shared membership_eligibility reader
# ships alongside the handler. The handler imports it lazily inside a try/except,
# so a missing file degrades to a logged warning rather than a cold-start
# ImportError — but see modules/gateway/infra/modules/budget-lambda/main.tf:80
# (#4391) for what happens when a shared module a handler needs is left out of the
# archive: the Lambda ImportErrors on cold start and the whole function stops.
data "archive_file" "pre_signup" {
  type        = "zip"
  output_path = "${path.module}/lambda/pre_signup.zip"

  # Issue #4848: the handler source lives at modules/gateway/lambda/pre-signup/,
  # where every other gateway Lambda lives and where the test harness
  # (tests/lambda/_handler_loader.py) already resolves. It used to live in this
  # Terraform module, which is build-output territory (pre_signup.zip is written
  # next to it and gitignored) and outside the collected test tree -- so the
  # packaged copy had no tests while an unpackaged duplicate had 18, and the two
  # silently drifted (#4849 landed shadow-mode code in one of them only).
  #
  # `filename` stays "pre_signup.py" even though the source is handler.py: it is
  # what the zip's internal module name must be for the `handler =
  # "pre_signup.handler"` setting on aws_lambda_function.pre_signup below to
  # resolve. Renaming either without the other is a
  # Runtime.ImportModuleError on every invocation, i.e. a sign-in outage. Setting
  # it explicitly is exactly why this is a multi-source archive rather than
  # `source_file`, which would name the entry handler.py and break the handler string.
  source {
    content  = file("${path.root}/../lambda/pre-signup/handler.py")
    filename = "pre_signup.py"
  }

  source {
    content  = file("${path.root}/../lambda/shared/membership_eligibility.py")
    filename = "membership_eligibility.py"
  }
}

# IAM Role for the Lambda function
resource "aws_iam_role" "pre_signup" {
  name = "${var.name_prefix}-pre-signup-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-pre-signup-role"
    Service = "iam"
    Purpose = "lambda-execution"
  })
}

# IAM Policy for Lambda to access CloudWatch Logs
resource "aws_iam_role_policy" "pre_signup_logs" {
  name = "${var.name_prefix}-pre-signup-logs"
  role = aws_iam_role.pre_signup.id

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
        Resource = "arn:aws:logs:${data.aws_region.current.id}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.name_prefix}-pre-signup-trigger:*"
      }
    ]
  })
}

# IAM Policy for Lambda to read from DynamoDB allowlist table
resource "aws_iam_role_policy" "pre_signup_dynamodb" {
  name = "${var.name_prefix}-pre-signup-dynamodb"
  role = aws_iam_role.pre_signup.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.signup_allowlist.arn,
          "${aws_dynamodb_table.signup_allowlist.arn}/index/*"
        ]
      },
      {
        Sid    = "DynamoDBKMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [var.kms_key_arn]
      }
      # Issue #4849: read the membership-eligibility projection (member_org_ids on
      # the identity-index rows). GetItem only — this Lambda is a reader; the
      # gateway API is the sole writer of these tables. ARNs arrive as a variable
      # rather than a cross-module reference: the tables live in the gateway ROOT
      # module, and referencing back into the root from here would close the
      # documented cloudfront -> api_gateway -> broker -> cloudfront dependency
      # loop (modules/gateway/infra/main.tf:723-736).
      # Conditional because an empty Resource list is a malformed policy, not an
      # empty grant — it fails the apply.
      ], length(var.identity_index_table_arns) > 0 ? [
      {
        Sid      = "IdentityIndexProjectionRead"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = var.identity_index_table_arns
      }
    ] : [])
  })
}

# IAM Policy for Lambda to read GitHub token from Secrets Manager
resource "aws_iam_role_policy" "pre_signup_secrets" {
  count = var.github_token_secret_arn != "" ? 1 : 0
  name  = "${var.name_prefix}-pre-signup-secrets"
  role  = aws_iam_role.pre_signup.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue"
        ]
        Resource = [var.github_token_secret_arn]
      }
    ]
  })
}

# Lambda Function
resource "aws_lambda_function" "pre_signup" {
  function_name                  = "${var.name_prefix}-pre-signup-trigger"
  description                    = "Cognito Pre Sign-Up trigger to gate GitHub user sign-ups"
  reserved_concurrent_executions = var.enable_reserved_concurrency ? 10 : -1

  filename         = data.archive_file.pre_signup.output_path
  source_code_hash = data.archive_file.pre_signup.output_base64sha256

  handler = "pre_signup.handler"
  runtime = "python3.12"

  role = aws_iam_role.pre_signup.arn

  timeout     = 10 # seconds (GitHub API calls may take a few seconds)
  memory_size = 128

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      ALLOWLIST_MODE          = var.pre_signup_allowlist_mode
      ALLOWED_ORGS            = var.pre_signup_allowed_orgs
      ALLOWLIST_TABLE         = aws_dynamodb_table.signup_allowlist.name
      GITHUB_TOKEN_SECRET_ARN = var.github_token_secret_arn
      LOG_LEVEL               = var.environment == "prod" ? "INFO" : "DEBUG"

      # Issue #4849: membership-eligibility projection tables (shadow-mode read).
      # Env var and code ship in this same apply — the ALLOWLIST_MODE /
      # ALLOW_OPEN_SIGNUP outage recorded in CLAUDE.md came from splitting them.
      IDENTITY_INDEX_TABLE        = var.identity_index_table_name
      USER_IDENTITY_INDEX_TABLE   = var.user_identity_index_table_name
      USER_IDENTITY_INDEX_V2_READ = var.user_identity_index_v2_read
    }
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-pre-signup-trigger"
    Service = "lambda"
    Purpose = "cognito-trigger"
  })
}

# CloudWatch Log Group for Lambda
resource "aws_cloudwatch_log_group" "pre_signup" {
  name              = "/aws/lambda/${var.name_prefix}-pre-signup-trigger"
  retention_in_days = var.environment == "prod" ? 30 : 7
  kms_key_id        = var.cloudwatch_kms_key_arn

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-pre-signup-trigger-logs"
    Service = "cloudwatch"
    Purpose = "lambda-logs"
  })
}

# Permission for Cognito to invoke the Lambda
resource "aws_lambda_permission" "cognito_pre_signup" {
  statement_id  = "AllowCognitoPreSignUpInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.pre_signup.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.main.arn
}

# =============================================================================
# DynamoDB Table for Signup Allowlist (Issue #314)
# =============================================================================

resource "aws_dynamodb_table" "signup_allowlist" {
  name         = "${var.name_prefix}-signup-allowlist"
  billing_mode = "PAY_PER_REQUEST"

  # Primary key: username (GitHub username or email, lowercased)
  hash_key = "username"

  attribute {
    name = "username"
    type = "S"
  }

  # Enable point-in-time recovery in prod
  point_in_time_recovery {
    enabled = var.environment == "prod" ? true : false
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-signup-allowlist"
    Service = "dynamodb"
    Purpose = "signup-access-control"
  })
}

# =============================================================================
# Outputs (Issue #314)
# =============================================================================

output "pre_signup_lambda_arn" {
  description = "ARN of the Pre Sign-Up Lambda function"
  value       = aws_lambda_function.pre_signup.arn
}

output "pre_signup_lambda_name" {
  description = "Name of the Pre Sign-Up Lambda function"
  value       = aws_lambda_function.pre_signup.function_name
}

output "signup_allowlist_table_name" {
  description = "Name of the DynamoDB signup allowlist table"
  value       = aws_dynamodb_table.signup_allowlist.name
}

output "signup_allowlist_table_arn" {
  description = "ARN of the DynamoDB signup allowlist table"
  value       = aws_dynamodb_table.signup_allowlist.arn
}
