# =============================================================================
# User Identity Index DynamoDB Table (Issue #537)
# =============================================================================
# Per-user lookup table: (provider, provider_user_id) → {user_id, org_id}
# Split from the existing identity-index table which retains only
# channel/tenant-level identities (github_installation_id, cognito_client_id).
# =============================================================================

resource "aws_dynamodb_table" "user_identity_index" {
  name         = "adp-${var.environment}-user-identity-index"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "provider"
  range_key    = "provider_user_id"

  attribute {
    name = "provider"
    type = "S"
  }

  attribute {
    name = "provider_user_id"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = merge(local.common_tags, {
    Name    = "adp-${var.environment}-user-identity-index"
    Service = "dynamodb"
    Purpose = "user-identity-index"
  })
}

resource "aws_ssm_parameter" "user_identity_index_table" {
  name        = "/adp/${var.environment}/gateway/user-identity-index-table"
  description = "DynamoDB table name for user-identity-index (Issue #537)"
  type        = "String"
  value       = aws_dynamodb_table.user_identity_index.name

  tags = local.common_tags
}

# IAM: Grant gateway IRSA role PutItem/GetItem/DeleteItem on new table.
# Matches the `aws_iam_role_policy.gateway_identity_index` pattern in main.tf
# (inline role policy, not a standalone aws_iam_policy + attachment) so the
# permissions actually land on the role.
resource "aws_iam_role_policy" "gateway_user_identity_index" {
  name = "${local.name_prefix}-policy-gateway-user-identity-index"
  role = local.gateway_service_irsa_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "UserIdentityIndexAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:DeleteItem"
        ]
        Resource = aws_dynamodb_table.user_identity_index.arn
      }
    ]
  })
}
