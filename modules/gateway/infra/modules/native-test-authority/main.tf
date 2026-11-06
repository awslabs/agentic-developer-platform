terraform {
  required_version = ">= 1.7"
  required_providers {
    aws = { source = "hashicorp/aws", version = ">= 6.0" }
  }
}

# Optional source module; no parent module instantiates it. No fixture,
# designation, consent, deployment registration or CI credential is created.
resource "aws_dynamodb_table" "authority" {
  name                        = var.table_name
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  range_key                   = "sk"
  deletion_protection_enabled = true

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
  # Deliberately no TTL. Designation tombstones and uncertain paid effects
  # outlive dispatch, cleanup, credential and Redis meter expiration.
}

locals {
  protected_keys = ["TOOLS_TEST_SUBJECT#*", "TOOLS_TEST_DEPLOYMENT#*"]
  gateway_policy = {
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "ReadAndFenceCurrentAuthority"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:ConditionCheckItem"]
        Resource = aws_dynamodb_table.authority.arn
      },
      {
        Sid      = "WriteTenantJournalsOnly"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.authority.arn
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["TENANT#*"] }
          "Null"                    = { "dynamodb:LeadingKeys" = "false" }
        }
      },
      {
        Sid      = "ReadOriginalRunInventory"
        Effect   = "Allow"
        Action   = ["dynamodb:Query"]
        Resource = aws_dynamodb_table.authority.arn
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["TENANT#*"] }
          "Null"                    = { "dynamodb:LeadingKeys" = "false" }
        }
      }
      ], var.execution_table_arn == "" ? [] : [{
        Sid      = "ReadOriginalExecutionProof"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem"]
        Resource = var.execution_table_arn
    }])
  }
}

# A table resource policy makes these denials effective even if the gateway or
# runner has another broad identity policy. The protected registrar must be a
# different role and must not be assumable by gateway, fixture or CI principals.
resource "aws_dynamodb_resource_policy" "protection" {
  resource_arn = aws_dynamodb_table.authority.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid       = "RetainAuthorityAndTombstones"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:PartiQLDelete"]
        Resource  = aws_dynamodb_table.authority.arn
      },
      {
        Sid       = "ProtectedRegistrationWriter"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:PartiQLInsert", "dynamodb:PartiQLUpdate"]
        Resource  = aws_dynamodb_table.authority.arn
        Condition = {
          "ForAnyValue:StringLike" = { "dynamodb:LeadingKeys" = local.protected_keys }
          "ArnNotEquals"           = { "aws:PrincipalArn" = var.registration_role_arn }
        }
      },
      {
        Sid       = "GatewayCannotChangeRegistration"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource  = aws_dynamodb_table.authority.arn
        Condition = {
          "ForAnyValue:StringNotLike" = { "dynamodb:LeadingKeys" = ["TENANT#*"] }
          "ArnEquals"                 = { "aws:PrincipalArn" = var.gateway_role_arn }
        }
      },
      {
        Sid       = "GatewayCannotChangeTableOrUseAlternateWrites"
        Effect    = "Deny"
        Principal = "*"
        NotAction = ["dynamodb:GetItem", "dynamodb:ConditionCheckItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource  = aws_dynamodb_table.authority.arn
        Condition = { "ArnEquals" = { "aws:PrincipalArn" = var.gateway_role_arn } }
      }
      ], length(var.ci_role_arns) == 0 ? [] : [{
        Sid       = "CINoAuthorityTableAccess"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["dynamodb:*"]
        Resource  = aws_dynamodb_table.authority.arn
        Condition = { "ArnEquals" = { "aws:PrincipalArn" = var.ci_role_arns } }
    }])
  })

  lifecycle {
    precondition {
      condition = (
        var.gateway_role_arn != var.registration_role_arn &&
        !contains(var.ci_role_arns, var.gateway_role_arn) &&
        !contains(var.ci_role_arns, var.registration_role_arn)
      )
      error_message = "Gateway, protected registrar and CI must use distinct roles."
    }
  }
}
