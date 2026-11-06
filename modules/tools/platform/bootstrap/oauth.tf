variable "gateway_oauth" {
  description = "The release root's gateway_oauth output, supplied through protected configuration; null disables outbound OAuth permissions."
  type = object({
    workload_identity_arn = string
    providers = map(object({
      provider_arn = string
      secret_id    = string
    }))
  })
  default = null
  validation {
    condition = var.gateway_oauth == null ? true : (
      can(regex("^arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:workload-identity-directory/default/workload-identity/adp-tools-[A-Za-z0-9_-]+$", var.gateway_oauth.workload_identity_arn)) &&
      length(var.gateway_oauth.providers) > 0 && alltrue([for provider in keys(var.gateway_oauth.providers) : contains(["github", "atlassian", "slack"], provider)]) &&
      alltrue([for vendor, provider in var.gateway_oauth.providers :
        provider.provider_arn == "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:token-vault/default/oauth2credentialprovider/adp-tools-${var.environment}-${vendor}-user" &&
        (can(regex("^[A-Za-z0-9/_+=.@-]+$", provider.secret_id)) || can(regex("^arn:aws:secretsmanager:${var.aws_region}:${var.aws_account_id}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{6}$", provider.secret_id)))
      ])
    )
    error_message = "Supply this gateway's workload identity and the configured environment-specific OAuth providers with same-account, same-region secret names or full ARNs."
  }
}

data "aws_secretsmanager_secret" "gateway_oauth" {
  for_each = var.gateway_oauth == null ? {} : var.gateway_oauth.providers
  arn      = startswith(each.value.secret_id, "arn:") ? each.value.secret_id : null
  name     = startswith(each.value.secret_id, "arn:") ? null : each.value.secret_id
}

data "aws_kms_key" "gateway_oauth_secret" {
  for_each = toset(compact([for secret in data.aws_secretsmanager_secret.gateway_oauth : secret.kms_key_id]))
  key_id   = each.value
}

resource "aws_iam_role_policy" "gateway_oauth" {
  count = var.gateway_oauth == null ? 0 : 1
  name  = "adp-tools-gateway-oauth"
  role  = aws_iam_role.gateway.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid    = "GatewayWorkloadToken"
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetWorkloadAccessToken"]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.aws_region}:${var.aws_account_id}:workload-identity-directory/default",
          var.gateway_oauth.workload_identity_arn
        ]
      },
      {
        Sid      = "VendorOauthTokens"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:GetResourceOauth2Token"]
        Resource = [for provider in var.gateway_oauth.providers : provider.provider_arn]
      },
      {
        Sid      = "ExternalOauthClientSecrets"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [for secret in data.aws_secretsmanager_secret.gateway_oauth : secret.arn]
      }
      ], length(data.aws_kms_key.gateway_oauth_secret) == 0 ? [] : [{
        Sid      = "DecryptExternalOauthClientSecrets"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [for key in data.aws_kms_key.gateway_oauth_secret : key.arn]
        Condition = {
          StringEquals = {
            "kms:ViaService"                  = "secretsmanager.${var.aws_region}.amazonaws.com"
            "kms:EncryptionContext:SecretARN" = [for secret in data.aws_secretsmanager_secret.gateway_oauth : secret.arn]
          }
        }
    }])
  })
}
