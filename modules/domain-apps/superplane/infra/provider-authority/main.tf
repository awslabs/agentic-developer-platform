terraform {
  required_version = ">= 1.9, < 2.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "= 6.65.0" }
  }
}

variable "environment" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "operator_role_arn" { type = string }
variable "operator_role_id" { type = string }
variable "provider_role_arn" { type = string }
variable "provider_role_id" { type = string }
variable "secret_arn" { type = string }
variable "child_boundary_arn" { type = string }
variable "managed_policy_arns" { type = set(string) }
variable "secret_kms_key_arns" {
  type    = set(string)
  default = []
}

data "aws_caller_identity" "current" {}
data "aws_iam_role" "operator" { name = basename(var.operator_role_arn) }
data "aws_iam_role" "provider" { name = basename(var.provider_role_arn) }
data "aws_iam_role" "gateway" { name = "adp-${var.environment}-role-gateway-service" }

locals {
  authority_name = "adp-${var.environment}-superplane-provider-authorities"
  evidence_name  = "adp-${var.environment}-superplane-provider-evidence"
  authority_arn  = "arn:aws:dynamodb:${var.region}:${var.account_id}:table/${local.authority_name}"
  evidence_arn   = "arn:aws:dynamodb:${var.region}:${var.account_id}:table/${local.evidence_name}"
  owner_tags = {
    ManagedBy   = "superplane-provider-authority"
    DomainApp   = "superplane"
    Environment = var.environment
  }
}

resource "terraform_data" "owner" {
  input = { operator = var.operator_role_id, provider = var.provider_role_id }
  lifecycle {
    precondition {
      condition = (
        can(regex("^[a-z][a-z0-9-]{0,19}$", var.environment)) &&
        data.aws_caller_identity.current.account_id == var.account_id &&
        split(":", data.aws_caller_identity.current.user_id)[0] == var.operator_role_id &&
        data.aws_iam_role.operator.arn == var.operator_role_arn && data.aws_iam_role.operator.unique_id == var.operator_role_id &&
        data.aws_iam_role.provider.arn == var.provider_role_arn && data.aws_iam_role.provider.unique_id == var.provider_role_id &&
        startswith(var.provider_role_arn, "arn:aws:iam::${var.account_id}:role/adp-${var.environment}-spp-") &&
        startswith(var.secret_arn, "arn:aws:secretsmanager:${var.region}:${var.account_id}:secret:") &&
        startswith(var.child_boundary_arn, "arn:aws:iam::${var.account_id}:policy/") &&
        length(var.managed_policy_arns) == 4 && toset([for suffix in ["network", "identity", "lifecycle", "state-validation"] : "${replace(var.provider_role_arn, ":role/", ":policy/")}-${suffix}"]) == var.managed_policy_arns &&
        alltrue([for arn in var.secret_kms_key_arns : startswith(arn, "arn:aws:kms:${var.region}:${var.account_id}:key/")])
      )
      error_message = "Selected account/operator/provider identities or exact same-account references differ."
    }
  }
}

resource "aws_dynamodb_table" "authority" {
  name         = local.authority_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "record_id"
  attribute {
    name = "record_id"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
  deletion_protection_enabled = true
  tags                        = local.owner_tags
  depends_on                  = [terraform_data.owner]
  lifecycle { prevent_destroy = true }
}

# Even an accidental Gateway identity-policy grant cannot promote validation
# writes into authority enrollment. Only the exact deployment operator writes.
resource "aws_dynamodb_resource_policy" "authority" {
  resource_arn = aws_dynamodb_table.authority.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyProtectedOwnerMayMutateAuthority"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:PartiQLInsert", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete"]
      Resource  = local.authority_arn
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = var.operator_role_arn } }
      }, {
      Sid       = "RecreatedOwnerCannotMutateAuthority"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:PartiQLInsert", "dynamodb:PartiQLUpdate", "dynamodb:PartiQLDelete"]
      Resource  = local.authority_arn
      Condition = { StringNotLike = { "aws:userid" = [var.operator_role_id, "${var.operator_role_id}:*"] } }
    }]
  })
}

resource "aws_dynamodb_table" "evidence" {
  name         = local.evidence_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "record_id"
  range_key    = "observed_at"
  attribute {
    name = "record_id"
    type = "S"
  }
  attribute {
    name = "observed_at"
    type = "S"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
  deletion_protection_enabled = true
  tags                        = local.owner_tags
  depends_on                  = [terraform_data.owner]
  lifecycle { prevent_destroy = true }
}

resource "aws_iam_policy" "gateway" {
  name = "adp-${var.environment}-superplane-provider-authority"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Sid = "ReadAndConditionAuthorityOnly", Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:ConditionCheckItem"], Resource = local.authority_arn },
      { Sid = "ImmutableProviderObservations", Effect = "Allow", Action = ["dynamodb:Query", "dynamodb:PutItem"], Resource = local.evidence_arn },
      { Sid = "ExactProviderSession", Effect = "Allow", Action = ["sts:AssumeRole", "sts:TagSession"], Resource = var.provider_role_arn },
      { Sid = "CurrentProviderRolePolicy", Effect = "Allow", Action = ["iam:GetRole", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:GetRolePolicy"], Resource = var.provider_role_arn },
      { Sid = "CurrentImmutableBoundaries", Effect = "Allow", Action = ["iam:GetPolicy", "iam:GetPolicyVersion"], Resource = concat([var.child_boundary_arn], sort(tolist(var.managed_policy_arns))) },
      { Sid = "ExactProviderTrustMaterial", Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"], Resource = var.secret_arn }
      ], length(var.secret_kms_key_arns) == 0 ? [] : [{
        Sid       = "DecryptExactProviderSecret", Effect = "Allow", Action = ["kms:Decrypt"], Resource = sort(tolist(var.secret_kms_key_arns)),
        Condition = { StringEquals = { "kms:ViaService" = "secretsmanager.${var.region}.amazonaws.com", "kms:EncryptionContext:SecretARN" = var.secret_arn } }
    }])
  })
  tags = local.owner_tags
}

resource "aws_iam_role_policy_attachment" "gateway" {
  role       = data.aws_iam_role.gateway.name
  policy_arn = aws_iam_policy.gateway.arn
}

output "gateway_environment" {
  value = {
    ADP_DOMAIN_PROVIDER_ACCOUNT_ID      = var.account_id
    ADP_DOMAIN_PROVIDER_AUTHORITY_TABLE = aws_dynamodb_table.authority.name
    ADP_DOMAIN_PROVIDER_EVIDENCE_TABLE  = aws_dynamodb_table.evidence.name
  }
  depends_on = [aws_iam_role_policy_attachment.gateway, aws_dynamodb_resource_policy.authority]
}
