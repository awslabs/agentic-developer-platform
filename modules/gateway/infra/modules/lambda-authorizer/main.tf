# =============================================================================
# Lambda Authorizer Module (Issue #239)
# =============================================================================
# Creates a Lambda authorizer for API Gateway that supports:
# 1. JWT validation against Cognito JWKS
# 2. IAM-based agent authentication via DynamoDB registry
#
# Architecture:
# API Gateway -> Lambda Authorizer -> Cognito JWKS / DynamoDB
#                                  -> Allow/Deny with context headers
# =============================================================================

# Get current AWS account ID and region
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# =============================================================================
# DynamoDB Agent Registry Table (Issue #248)
# =============================================================================
# Stores agent configurations mapping IAM role ARNs to agent identity.
# Issue #248: Changed from role_arn as PK to agent_id (UUID) as PK.
# - Partition key: agent_id (UUID)
# - GSI: by-role-arn (partition=role_arn) - for Lambda authorizer lookup
# - GSI: by-org-team (partition=org_id, sort=team_id)
# - GSI: by-owner (partition=owner)

resource "aws_dynamodb_table" "agent_registry" {
  name         = "${var.name_prefix}-agent-registry"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "agent_id"

  # Primary key: agent_id (UUID)
  attribute {
    name = "agent_id"
    type = "S"
  }

  # GSI: by-role-arn attribute
  attribute {
    name = "role_arn"
    type = "S"
  }

  # GSI: by-org-team attributes
  attribute {
    name = "org_id"
    type = "S"
  }

  attribute {
    name = "team_id"
    type = "S"
  }

  # GSI: by-owner attribute
  attribute {
    name = "owner"
    type = "S"
  }

  # GSI: by-role-arn - for Lambda authorizer lookup (Issue #248)
  global_secondary_index {
    name            = "by-role-arn"
    hash_key        = "role_arn"
    projection_type = "ALL"
  }

  # GSI: by-org-team - for querying agents by organization and team
  global_secondary_index {
    name            = "by-org-team"
    hash_key        = "org_id"
    range_key       = "team_id"
    projection_type = "ALL"
  }

  # GSI: by-owner - for querying agents by owner
  global_secondary_index {
    name            = "by-owner"
    hash_key        = "owner"
    projection_type = "ALL"
  }

  # Point-in-time recovery
  point_in_time_recovery {
    enabled = true
  }

  # Server-side encryption with customer-managed KMS key (CKV_AWS_119)
  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-agent-registry"
    Service = "dynamodb"
    Purpose = "agent-registry"
  })
}

# Seed the table with a test agent entry (Issue #248: now uses agent_id as PK)
resource "aws_dynamodb_table_item" "test_agent" {
  table_name = aws_dynamodb_table.agent_registry.name
  hash_key   = aws_dynamodb_table.agent_registry.hash_key

  # Issue #248: agent_id is now the primary key (UUID), role_arn is in GSI
  item = jsonencode({
    agent_id = {
      S = "00000000-0000-0000-0000-000000000001"
    }
    role_arn = {
      S = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/${var.name_prefix}-test-agent"
    }
    agent_name = {
      S = "test-agent"
    }
    org_id = {
      S = "default"
    }
    team_id = {
      S = "platform"
    }
    owner = {
      S = "system"
    }
    scope = {
      S = "shared"
    }
    budget_config_id = {
      S = ""
    }
    allowed_models = {
      SS = ["claude-sonnet", "claude-haiku"]
    }
    status = {
      S = "active"
    }
    description = {
      S = "Test agent for development and CI/CD"
    }
    image_uri = {
      S = ""
    }
    code_repo = {
      S = ""
    }
    workflow_name = {
      S = ""
    }
    created_at = {
      S = timestamp()
    }
    updated_at = {
      S = timestamp()
    }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

# Issue #1108: Seed deploy-runner entry for platform SigV4 calls to /internal/*
resource "aws_dynamodb_table_item" "deploy_runner" {
  table_name = aws_dynamodb_table.agent_registry.name
  hash_key   = aws_dynamodb_table.agent_registry.hash_key

  item = jsonencode({
    agent_id = {
      S = "00000000-0000-0000-0000-000000000002"
    }
    role_arn = {
      S = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/adp-${var.environment}-agent-runner-role"
    }
    agent_name = {
      S = "deploy-runner"
    }
    org_id = {
      S = "platform"
    }
    team_id = {
      S = "platform"
    }
    owner = {
      S = "system"
    }
    scope = {
      S = "platform"
    }
    budget_config_id = {
      S = ""
    }
    allowed_models = {
      SS = ["claude-sonnet"]
    }
    status = {
      S = "active"
    }
    description = {
      S = "Platform deploy runner — calls /internal/v1/credential-assume-role on customer-deploy workflows"
    }
    image_uri = {
      S = ""
    }
    code_repo = {
      S = ""
    }
    workflow_name = {
      S = ""
    }
    created_at = {
      S = timestamp()
    }
    updated_at = {
      S = timestamp()
    }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

# =============================================================================
# Lambda Layer for PyJWT (S3-sourced — Issue #408)
# =============================================================================
# The layer zip is built by CodeBuild (adp-dev-pyjwt-layer project) and
# uploaded to S3. Terraform references it via data source — no Docker daemon
# required at apply time.

data "aws_s3_object" "pyjwt_layer" {
  bucket = var.lambda_artifact_bucket
  key    = "lambda-layers/pyjwt-py313.zip"
}

resource "aws_lambda_layer_version" "pyjwt" {
  layer_name          = "${var.name_prefix}-pyjwt-py313"
  description         = "PyJWT[crypto] for Python 3.13 (x86_64) - JWT validation"
  s3_bucket           = var.lambda_artifact_bucket
  s3_key              = "lambda-layers/pyjwt-py313.zip"
  source_code_hash    = data.aws_s3_object.pyjwt_layer.etag
  compatible_runtimes = ["python3.13"]

  compatible_architectures = ["x86_64"]

  lifecycle {
    create_before_destroy = true
    # Layer content is managed by CodeBuild workflow; don't replace on etag drift
    # during plan if the zip hasn't actually changed semantically.
    ignore_changes = [source_code_hash]
  }
}

# =============================================================================
# Lambda Authorizer Function
# =============================================================================

data "archive_file" "authorizer" {
  type        = "zip"
  output_path = "${path.module}/authorizer.zip"

  source {
    content  = file("${path.root}/../lambda/api-authorizer/handler.py")
    filename = "handler.py"
  }
}

resource "aws_lambda_function" "authorizer" {
  function_name                  = "${var.name_prefix}-api-authorizer"
  description                    = "API Gateway Lambda Authorizer for JWT and IAM auth (Issue #239)"
  reserved_concurrent_executions = 50

  filename         = data.archive_file.authorizer.output_path
  source_code_hash = data.archive_file.authorizer.output_base64sha256

  handler       = "handler.lambda_handler"
  runtime       = "python3.13"
  architectures = ["x86_64"]
  memory_size   = var.lambda_memory_size
  timeout       = var.lambda_timeout

  role   = aws_iam_role.authorizer.arn
  layers = [aws_lambda_layer_version.pyjwt.arn]

  tracing_config {
    mode = "Active"
  }

  environment {
    variables = {
      COGNITO_USER_POOL_ID = var.cognito_user_pool_id
      COGNITO_REGION       = var.aws_region
      AGENT_REGISTRY_TABLE = aws_dynamodb_table.agent_registry.name
      AUTHORIZER_CACHE_TTL = tostring(var.authorizer_cache_ttl)
    }
  }

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-authorizer"
    Service = "lambda"
    Purpose = "api-gateway-authorizer"
  })

  depends_on = [
    aws_cloudwatch_log_group.authorizer,
  ]
}

# =============================================================================
# Lambda IAM Role and Policy
# =============================================================================

resource "aws_iam_role" "authorizer" {
  name = "${var.name_prefix}-api-authorizer-role"

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
    Name    = "${var.name_prefix}-api-authorizer-role"
    Service = "iam"
    Purpose = "api-gateway-authorizer"
  })
}

resource "aws_iam_role_policy" "authorizer" {
  name = "${var.name_prefix}-api-authorizer-policy"
  role = aws_iam_role.authorizer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # DynamoDB read access for agent registry (Issue #248: added Query for GSI)
      {
        Sid    = "DynamoDBReadAgentRegistry"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query"
        ]
        Resource = [
          aws_dynamodb_table.agent_registry.arn,
          "${aws_dynamodb_table.agent_registry.arn}/index/*"
        ]
      },
      # KMS access for DynamoDB encryption (CKV_AWS_119)
      {
        Sid    = "DynamoDBKMSAccess"
        Effect = "Allow"
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey"
        ]
        Resource = [var.kms_key_arn]
      },
      # CloudWatch Logs
      {
        Sid    = "CloudWatchLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.name_prefix}-api-authorizer:*"
      }
    ]
  })
}

# =============================================================================
# CloudWatch Log Group
# =============================================================================

resource "aws_cloudwatch_log_group" "authorizer" {
  name              = "/aws/lambda/${var.name_prefix}-api-authorizer"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.cloudwatch_kms_key_arn

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-authorizer-logs"
    Service = "cloudwatch"
    Purpose = "api-gateway-authorizer"
  })
}

# =============================================================================
# Lambda Permission for API Gateway
# =============================================================================

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.authorizer.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${var.api_gateway_execution_arn}/*"
}

# =============================================================================
# API Gateway Authorizer (DEPRECATED for Dual-Path Architecture - Issue #260)
# =============================================================================
# REQUEST type authorizer that receives the full request context including
# headers and IAM identity. This allows both JWT (via Authorization header)
# and IAM SigV4 authentication (via requestContext.identity).
# Caches authorization results for the configured TTL.
#
# Issue #260: This authorizer is NOT attached to any methods in the dual-path
# architecture. Instead:
# - /{proxy+}        → NONE auth (FastAPI validates JWT directly)
# - /agent/{proxy+}  → AWS_IAM auth (API Gateway validates SigV4 natively)
#
# The Lambda authorizer resource is preserved for potential future use but is
# not actively used. The backend-deploy workflow no longer wires it to methods.

resource "aws_api_gateway_authorizer" "main" {
  name                   = "${var.name_prefix}-lambda-authorizer"
  rest_api_id            = var.api_gateway_id
  authorizer_uri         = aws_lambda_function.authorizer.invoke_arn
  authorizer_credentials = aws_iam_role.authorizer_invocation.arn
  type                   = "REQUEST"
  identity_source        = "method.request.header.Authorization,context.identity.userArn"

  # Cache authorizer results
  authorizer_result_ttl_in_seconds = var.authorizer_cache_ttl
}

# IAM role for API Gateway to invoke the Lambda authorizer
resource "aws_iam_role" "authorizer_invocation" {
  name = "${var.name_prefix}-api-authorizer-invoke-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "apigateway.amazonaws.com"
        }
      }
    ]
  })

  tags = merge(var.common_tags, {
    Name    = "${var.name_prefix}-api-authorizer-invoke-role"
    Service = "iam"
    Purpose = "api-gateway-authorizer-invocation"
  })
}

resource "aws_iam_role_policy" "authorizer_invocation" {
  name = "${var.name_prefix}-api-authorizer-invoke-policy"
  role = aws_iam_role.authorizer_invocation.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InvokeLambdaAuthorizer"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.authorizer.arn
      }
    ]
  })
}
