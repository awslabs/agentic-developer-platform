# Handoff creates the same private MCP targets as the existing tools release.
# Reuse its exact network policy and AgentCore key-validation conditions.
locals {
  tools_handoff_private_target_create = (
    var.enable_tools_handoff_deployment && var.enable_tools_handoff_preparation &&
    var.tools_knowledge_target_creation != null
  )
  tools_handoff_gateway_key_enabled = (
    var.enable_tools_handoff_deployment && var.enable_tools_handoff_preparation &&
    var.tools_gateway_kms_key_arn != ""
  )
  tools_handoff_target_actions = distinct(concat(
    local.tools_handoff_private_target_create ? local.tools_private_target_actions : [],
    local.tools_handoff_gateway_key_enabled ? ["kms:Decrypt", "kms:GenerateDataKey", "kms:ListGrants"] : [],
  ))
  tools_handoff_data_keys = concat(
    [var.tools_gateway_kms_key_arn],
    var.tools_handoff_state_kms_key_arn == "" ? [] : [var.tools_handoff_state_kms_key_arn],
  )
  tools_handoff_gateway_key_policy = jsonencode({ Version = "2012-10-17", Statement = concat(
    [for statement in jsondecode(local.tools_gateway_key_policy).Statement : statement
      if statement.Sid != "DenyOtherGatewayKeyResources"
    ],
    [
      # The handoff also writes its encrypted S3 journal. Keep that separate key
      # usable through its existing S3-only grant, while fencing all other keys.
      { Sid = "DenyOtherHandoffDataKeys", Effect = "Deny", Action = ["kms:Decrypt", "kms:GenerateDataKey"], NotResource = local.tools_handoff_data_keys },
      { Sid = "DenyOtherGatewayGrantResources", Effect = "Deny", Action = "kms:ListGrants", NotResource = var.tools_gateway_kms_key_arn },
    ],
  ) })
}

resource "aws_iam_role_policy_attachment" "tools_handoff_private_target" {
  count      = local.tools_handoff_private_target_create ? 1 : 0
  role       = aws_iam_role.tools_handoff[0].name
  policy_arn = aws_iam_policy.tools_knowledge_creation[0].arn
}

resource "aws_iam_policy" "tools_handoff_gateway_key" {
  count       = local.tools_handoff_gateway_key_enabled ? 1 : 0
  name        = "${var.name_prefix}-tools-handoff-gateway-key"
  description = "Scoped AgentCore gateway key validation for connector handoff"
  policy      = local.tools_handoff_gateway_key_policy
  lifecycle {
    precondition {
      condition = (
        var.tools_gateway_kms_key_arn != var.tools_handoff_state_kms_key_arn &&
        var.tools_gateway_kms_key_arn != local.tools_lambda_environment_key
      )
      error_message = "Gateway target validation must use a separate key from the S3 journal and Lambda environments."
    }
    precondition {
      condition     = length(local.tools_handoff_gateway_key_policy) <= 6144
      error_message = "Handoff gateway key permissions exceed the managed-policy quota."
    }
  }
}

resource "aws_iam_role_policy_attachment" "tools_handoff_gateway_key" {
  count      = local.tools_handoff_gateway_key_enabled ? 1 : 0
  role       = aws_iam_role.tools_handoff[0].name
  policy_arn = aws_iam_policy.tools_handoff_gateway_key[0].arn
}
