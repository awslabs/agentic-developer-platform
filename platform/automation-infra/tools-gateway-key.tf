# AgentCore validates the key on behalf of the release caller before using
# the gateway service role to encrypt target configuration.
# https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-encryption.html
# The bootstrap owner retains key administration; this optional grant is only
# key validation through AgentCore for the existing gateway.
variable "tools_gateway_kms_key_arn" {
  description = "Optional exact customer-managed key ARN verified against the existing tools gateway. Only AgentCore key validation and grant metadata reads; no key or grant administration."
  type        = string
  default     = ""
  validation {
    condition = var.tools_gateway_kms_key_arn == "" || (
      (var.enable_tools_deployment || (var.enable_tools_handoff_deployment && var.enable_tools_handoff_preparation)) &&
      can(regex("^arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:key/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", var.tools_gateway_kms_key_arn))
    )
    error_message = "Enable tools deployment or handoff preparation and select one exact gateway encryption key in this account and region."
  }
}

locals {
  tools_gateway_key_enabled = var.enable_tools_deployment && var.tools_gateway_kms_key_arn != ""
  tools_gateway_key_actions = local.tools_gateway_key_enabled ? ["kms:Decrypt", "kms:GenerateDataKey", "kms:ListGrants"] : []
  tools_gateway_key_service = "bedrock-agentcore.${var.aws_region}.amazonaws.com"
  tools_gateway_key_policy = jsonencode({ Version = "2012-10-17", Statement = [
    # CreateGatewayTarget generates and decrypts a test key without encryption
    # context as its caller, then reads existing grant metadata. Actual target
    # encryption/decryption runs under the separately governed gateway role.
    { Sid = "GatewayKeyValidation", Effect = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = var.tools_gateway_kms_key_arn,
      Condition = {
        StringEquals = { "kms:ViaService" = local.tools_gateway_key_service, "kms:CallerAccount" = local.tools_account }
        Null         = { "kms:EncryptionContextKeys" = "true" }
      }
    },
    { Sid = "GatewayGrantRead", Effect = "Allow", Action = ["kms:ListGrants"], Resource = var.tools_gateway_kms_key_arn,
    Condition = { StringEquals = { "kms:ViaService" = local.tools_gateway_key_service, "kms:CallerAccount" = local.tools_account } } },
    # Explicit fences also constrain any independent key-policy Allow. The
    # shared inline Decrypt fence retains the separate Lambda environment key.
    { Sid = "DenyOtherGatewayKeyResources", Effect = "Deny", Action = ["kms:GenerateDataKey", "kms:ListGrants"], NotResource = var.tools_gateway_kms_key_arn },
    { Sid = "DenyNonAgentCoreGatewayKeyUse", Effect = "Deny", Action = ["kms:Decrypt", "kms:GenerateDataKey", "kms:ListGrants"], Resource = var.tools_gateway_kms_key_arn,
    Condition = { StringNotEquals = { "kms:ViaService" = local.tools_gateway_key_service } } },
    { Sid = "DenyGatewayKeyEncryptionContext", Effect = "Deny", Action = ["kms:Decrypt", "kms:GenerateDataKey"], Resource = var.tools_gateway_kms_key_arn,
    Condition = { Null = { "kms:EncryptionContextKeys" = "false" } } },
    { Sid = "DenyOtherGatewayKeyCaller", Effect = "Deny", Action = ["kms:Decrypt", "kms:GenerateDataKey", "kms:ListGrants"], Resource = var.tools_gateway_kms_key_arn,
    Condition = { StringNotEquals = { "kms:CallerAccount" = local.tools_account } } },
  ] })
}

resource "aws_iam_policy" "tools_gateway_key" {
  count  = local.tools_gateway_key_enabled ? 1 : 0
  name   = "${var.name_prefix}-tools-gateway-key"
  policy = local.tools_gateway_key_policy
  lifecycle {
    precondition {
      condition     = var.tools_gateway_kms_key_arn != local.tools_lambda_environment_key
      error_message = "Gateway configuration must use its customer-managed key, not the Lambda environment key."
    }
    precondition {
      condition     = length(local.tools_gateway_key_policy) <= 6144
      error_message = "Gateway key grants exceed the managed-policy quota."
    }
  }
}

resource "aws_iam_role_policy_attachment" "tools_gateway_key" {
  count      = local.tools_gateway_key_enabled ? 1 : 0
  role       = aws_iam_role.tools_deployment[0].name
  policy_arn = aws_iam_policy.tools_gateway_key[0].arn
}
