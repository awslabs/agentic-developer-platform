resource "aws_ecr_repository" "facade" {
  name                 = "adp-tools-facade"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_dynamodb_table" "executions" {
  name         = "adp-tools-${var.environment}-executions"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "execution_id"
  attribute {
    name = "execution_id"
    type = "S"
  }
  attribute {
    name = "cleanup_partition"
    type = "S"
  }
  attribute {
    name = "next_cleanup_at"
    type = "N"
  }
  global_secondary_index {
    name            = "cleanup-due"
    hash_key        = "cleanup_partition"
    range_key       = "next_cleanup_at"
    projection_type = "ALL"
  }
  server_side_encryption {
    enabled = true
  }
  point_in_time_recovery {
    enabled = true
  }
  ttl {
    attribute_name = "retention_ttl"
    enabled        = true
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_key" "gateway" {
  description             = "AgentCore tools gateway (${var.environment})"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowAccountIAM"
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }
      Action    = "kms:*"
      Resource  = "*"
    }]
  })
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_key" "assertion" {
  # The façade signs every tool call; the key must exist as soon as the façade
  # identity does, not only once an interceptor verifies the signatures. Its
  # policy names the façade role, which must therefore exist first.
  depends_on               = [aws_iam_role.facade]
  count                    = local.assertion_key_count
  description              = "Tools principal assertion signing (${var.environment})"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_2048"
  deletion_window_in_days  = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid       = "AllowAccountIAM"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }
        Action    = ["kms:Create*", "kms:Describe*", "kms:Enable*", "kms:List*", "kms:Put*", "kms:Update*", "kms:Revoke*", "kms:Disable*", "kms:Get*", "kms:Delete*", "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion"]
        Resource  = "*"
      },
      {
        Sid       = "SignFromFacadeOnly"
        Effect    = "Allow"
        Principal = { AWS = var.tools_facade_role_arn }
        Action    = "kms:Sign"
        Resource  = "*"
      },
      ], local.interceptor_verification_role_arn != "" ? [
      {
        Sid       = "VerifyFromInterceptorOnly"
        Effect    = "Allow"
        Principal = { AWS = local.interceptor_verification_role_arn }
        Action    = ["kms:Verify", "kms:DescribeKey"]
        Resource  = "*"
      }
      ] : [], var.interceptor_lambda_arn != "" ? [
      {
        Sid       = "VerifyFromSharedToolsOnly"
        Effect    = "Allow"
        Principal = { AWS = data.aws_lambda_function.shared_tools[0].role }
        Action    = "kms:Verify"
        Resource  = "*"
      }
      ] : [], var.tools_browser_consumer_role_arn != "" ? [
      {
        Sid       = "VerifyFromBrowserConsumerOnly"
        Effect    = "Allow"
        Principal = { AWS = var.tools_browser_consumer_role_arn }
        Action    = "kms:Verify"
        Resource  = "*"
      }
      ] : [], var.credential_target_url != "" ? [
      {
        Sid       = "VerifyFromBrokerOnly"
        Effect    = "Allow"
        Principal = { AWS = var.credential_broker_role_arn }
        Action    = "kms:Verify"
        Resource  = "*"
      }
      ] : [], var.knowledge_door_endpoint != "" ? [
      {
        Sid       = "VerifyFromKnowledgeMediatorOnly"
        Effect    = "Allow"
        Principal = { AWS = var.knowledge_mediator_role_arn }
        Action    = "kms:Verify"
        Resource  = "*"
      }
    ] : [])
  })
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_key" "reconciler" {
  count                    = local.facade_enabled ? 1 : 0
  description              = "Tools cleanup-only assertion signing (${var.environment})"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "RSA_2048"
  deletion_window_in_days  = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid       = "AccountManagement", Effect = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:root" }
        Action    = ["kms:Create*", "kms:Describe*", "kms:Enable*", "kms:List*", "kms:Put*", "kms:Update*", "kms:Revoke*", "kms:Disable*", "kms:Get*", "kms:Delete*", "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion"]
        Resource  = "*"
      },
      {
        Sid       = "SignFromReconcilerOnly", Effect = "Allow"
        Principal = { AWS = "arn:aws:iam::${var.aws_account_id}:role/adp-${var.environment}-role-tools-reconciler" }
        Action    = "kms:Sign", Resource = "*"
      }
      ], var.interceptor_lambda_arn != "" ? [{
        Sid       = "VerifyFromSharedToolsOnly", Effect = "Allow"
        Principal = { AWS = data.aws_lambda_function.shared_tools[0].role }
        Action    = "kms:Verify", Resource = "*"
    }] : [])
  })
  lifecycle { prevent_destroy = true }
}

resource "aws_ssm_parameter" "reconciler_signing_key" {
  count = local.facade_enabled ? 1 : 0
  name  = "/adp/${var.environment}/tools/reconciler-signing-key-arn"
  type  = "String"
  value = aws_kms_key.reconciler[0].arn
}

resource "aws_iam_role_policy" "reconciler_provider" {
  count = local.facade_enabled ? 1 : 0
  name  = "adp-tools-cleanup-only-provider"
  role  = "adp-${var.environment}-role-tools-reconciler"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "kms:Sign", Resource = aws_kms_key.reconciler[0].arn },
    { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:${local.shared_tools_function_name}" },
    { Effect = "Allow", Action = "execute-api:Invoke", Resource = "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/native-cleanup" },
    { Effect = "Allow", Action = "execute-api:Invoke", Resource = "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/outcome-repair" },
    { Effect = "Allow", Action = "execute-api:Invoke", Resource = "${var.gateway_internal_api_execution_arn}/POST/internal/v1/tools/atlassian/reconcile" }
  ] })
}

resource "aws_kms_alias" "gateway" {
  name          = "alias/${var.kms_key_alias != "" ? var.kms_key_alias : "adp-tools-${var.environment}"}"
  target_key_id = aws_kms_key.gateway.key_id
}

resource "aws_iam_role" "gateway" {
  name = "adp-tools-${var.environment}-gateway"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.aws_account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:gateway/adp-tools-*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "gateway_kms" {
  name = "adp-tools-gateway-kms"
  role = aws_iam_role.gateway.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["kms:Decrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
      Resource = aws_kms_key.gateway.arn
    }]
  })
}

resource "aws_iam_role_policy" "gateway_targets" {
  count = length(local.tool_targets) > 0 ? 1 : 0
  name  = "adp-tools-gateway-targets"
  role  = aws_iam_role.gateway.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = sort(distinct([for target in values(local.tool_targets) : target.lambda_arn]))
    }]
  })
}

resource "aws_iam_role_policy" "gateway_interceptor" {
  count = var.enable_interceptor || var.interceptor_lambda_arn != "" ? 1 : 0
  name  = "adp-tools-gateway-interceptor"
  role  = aws_iam_role.gateway.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = var.enable_interceptor ? "arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:adp-tools-${var.environment}-interceptor:live" : var.interceptor_lambda_arn
    }]
  })
}

resource "aws_iam_role_policy" "credential_target" {
  count = var.credential_target_url == "" ? 0 : 1
  name  = "adp-tools-gateway-credential-target"
  role  = aws_iam_role.gateway.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "execute-api:Invoke"
      Resource = [
        "${var.credential_target_api_arn}/GET/internal/v1/user-credentials",
        "${var.credential_target_api_arn}/POST/internal/v1/proxy-request",
        "${var.credential_target_api_arn}/POST/internal/v1/credential-assume-role",
        "${var.credential_target_api_arn}/POST/internal/v1/credential-materialize",
      ]
    }]
  })
}

resource "aws_iam_role_policy" "credential_broker_verify" {
  count = var.credential_target_url == "" ? 0 : 1
  name  = "adp-tools-credential-broker-verify"
  role  = element(reverse(split("/", var.credential_broker_role_arn)), 0)
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "kms:Verify", Resource = aws_kms_key.assertion[0].arn
  }] })
}
