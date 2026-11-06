terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "6.68.0" }
  }
}

variable "enabled" {
  type    = bool
  default = false
}
variable "environment" {
  type = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,31}$", var.environment))
    error_message = "Use a bounded deployment environment name."
  }
}
variable "account_id" {
  type = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "Use the confirmed AWS account."
  }
}
variable "gateway_role_arn" {
  type = string
  validation {
    condition     = var.gateway_role_arn == "arn:aws:iam::${var.account_id}:role/adp-${var.environment}-role-gateway-service"
    error_message = "Only the selected Gateway service role may sign native deliveries."
  }
}
variable "issuer_origin" {
  type = string
  validation {
    condition     = can(regex("^https://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?$", var.issuer_origin))
    error_message = "Use the exact public HTTPS issuer origin, without a path or credentials."
  }
}
variable "approval_origin" {
  type = string
  validation {
    condition     = can(regex("^https://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?$", var.approval_origin))
    error_message = "Use the exact browser HTTPS origin, without a path or credentials."
  }
}
variable "tags" {
  type    = map(string)
  default = {}
}

resource "aws_kms_key" "issuer" {
  count                    = var.enabled ? 1 : 0
  description              = "Private native-human credential envelope signer for ADP ${var.environment}"
  customer_master_key_spec = "RSA_3072"
  key_usage                = "SIGN_VERIFY"
  deletion_window_in_days  = 30
  # Asymmetric keys do not support automatic key rotation. Consumers pin the
  # immutable ARN/public key; replacement requires a separately reviewed cutover.
  enable_key_rotation = false
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "AccountAdministration", Effect = "Allow", Principal = { AWS = "arn:aws:iam::${var.account_id}:root" }, Action = "kms:*", Resource = "*" },
      { Sid = "GatewayPublicKey", Effect = "Allow", Principal = { AWS = var.gateway_role_arn }, Action = "kms:GetPublicKey", Resource = "*" },
      { Sid = "GatewayEnvelopeSigning", Effect = "Allow", Principal = { AWS = var.gateway_role_arn }, Action = "kms:Sign", Resource = "*",
      Condition = { StringEquals = { "kms:SigningAlgorithm" = "RSASSA_PSS_SHA_256" } } },
      { Sid = "NoOtherEnvelopeSigner", Effect = "Deny", Principal = "*", Action = "kms:Sign", Resource = "*",
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = var.gateway_role_arn } } },
      { Sid = "OnlyNativeWireAlgorithm", Effect = "Deny", Principal = "*", Action = "kms:Sign", Resource = "*",
      Condition = { StringNotEquals = { "kms:SigningAlgorithm" = "RSASSA_PSS_SHA_256" } } }
    ]
  })
  tags = merge(var.tags, { Purpose = "native-private-credential-signing" })
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_ssm_parameter" "configuration" {
  name = "/adp/${var.environment}/gateway/native-credentials-config"
  type = "String"
  value = jsonencode({
    enabled         = var.enabled
    issuer          = var.issuer_origin
    approval_origin = var.approval_origin
    key_id          = "adp-native-private-v1"
    key_arn         = var.enabled ? aws_kms_key.issuer[0].arn : ""
  })
  tags = var.tags
}

output "key_arn" {
  value = var.enabled ? aws_kms_key.issuer[0].arn : ""
}
