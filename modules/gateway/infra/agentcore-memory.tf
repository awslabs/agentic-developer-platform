# =============================================================================
# Gateway-owned shared AgentCore user memory (#7901; implementation #7210 / #7547)
# =============================================================================
# The gateway's agentcore_recall() (src/agentauth/chat_model.py) reads four env
# vars and treats "all empty" as disabled and "partially set" as an outage:
#
#   ADP_AGENTCORE_MEMORY_ID            <- SSM /adp/<env>/gateway/agentcore-memory-id
#   ADP_AGENTCORE_MEMORY_STRATEGY_ID   <- SSM /adp/<env>/gateway/agentcore-memory-strategy-id
#   ADP_AGENTCORE_MEMORY_REGION        <- SSM /adp/<env>/gateway/agentcore-memory-region
#                                         (must equal the pod's AWS_REGION)
#   ADP_AGENTCORE_NAMESPACE_KEY        <- SSM SecureString /adp/<env>/gateway/agentcore-namespace-key
#                                         (hex, >= 32 bytes; must reach the pod via
#                                         the gateway Secret, never the ConfigMap)
#
# Everything here is created only when agentcore_memory_enabled is true, so a
# fresh environment is unchanged until an operator flips the switch in
# environments/<env>/modules/gateway.tfvars. The renderers (deploy-all.sh Step 4
# and gateway-deploy.yml) are not changed by this file; until they pass the env
# vars through, the gateway keeps memory disabled exactly as today.
#
# Owner namespaces: agentcore_recall.owner_namespace() returns
# "/adp/owners/<hmac-sha256(namespace_key, [tenant_id, user_id])>" and every
# CreateEvent uses that last path segment as actorId, so the strategy's namespace
# template must be exactly "/adp/owners/{actorId}" for extracted records to land
# where recall and management look for them (and for the `namespaces ==
# [namespace]` validation to accept them).
#
# Disabling the switch destroys the memory and every extracted record; enabling
# it again mints a new namespace key, so previously extracted records become
# unreachable by design. Do not rotate the key by hand for the same reason.

variable "agentcore_memory_enabled" {
  type        = bool
  description = "Provision the gateway-owned AgentCore memory, its strategy, the gateway IAM grant and the SSM parameters agentcore_recall() expects. Operator switch; default off."
  default     = false
}

variable "agentcore_memory_event_expiry_days" {
  type        = number
  description = "Days before raw AgentCore memory events expire (AgentCore accepts 3-365). Extracted long-term records are unaffected."
  default     = 90
  validation {
    condition     = var.agentcore_memory_event_expiry_days >= 3 && var.agentcore_memory_event_expiry_days <= 365
    error_message = "agentcore_memory_event_expiry_days must be between 3 and 365."
  }
}

variable "agentcore_memory_strategy_type" {
  type        = string
  description = "Built-in long-term extraction strategy. SEMANTIC (facts, decisions and corrections) or USER_PREFERENCE; both yield one record namespace per owner, which the gateway requires. SUMMARIZATION is per-session and incompatible."
  default     = "SEMANTIC"
  validation {
    condition     = contains(["SEMANTIC", "USER_PREFERENCE"], var.agentcore_memory_strategy_type)
    error_message = "agentcore_memory_strategy_type must be SEMANTIC or USER_PREFERENCE."
  }
}

locals {
  agentcore_memory_count = var.agentcore_memory_enabled ? 1 : 0
  # AgentCore memory names must match [a-zA-Z][a-zA-Z0-9_]{0,47}: no hyphens.
  agentcore_memory_name            = "adp_${var.environment}_gateway_user_memory"
  agentcore_memory_namespace_owner = "/adp/owners/{actorId}"
}

# -----------------------------------------------------------------------------
# Execution role: AgentCore invokes Bedrock models to extract long-term records.
# -----------------------------------------------------------------------------
data "aws_iam_policy_document" "agentcore_memory_execution_trust" {
  count = local.agentcore_memory_count

  statement {
    sid     = "AgentCoreMemoryAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:memory/*"]
    }
  }
}

resource "aws_iam_role" "agentcore_memory_execution" {
  count = local.agentcore_memory_count

  name               = "${local.name_prefix}-agentcore-memory-execution"
  assume_role_policy = data.aws_iam_policy_document.agentcore_memory_execution_trust[0].json

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-agentcore-memory-execution"
    Service = "bedrock-agentcore"
    Purpose = "memory-extraction"
  })
}

resource "aws_iam_role_policy" "agentcore_memory_execution" {
  count = local.agentcore_memory_count

  name = "${local.name_prefix}-agentcore-memory-model-inference"
  role = aws_iam_role.agentcore_memory_execution[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ExtractionModelInference"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = ["arn:${data.aws_partition.current.partition}:bedrock:*::foundation-model/*"]
      }
    ]
  })
}

# CreateMemory validates that it can assume the execution role; a role created in
# the same apply is not always visible yet (same pattern as orchestration-tick).
resource "time_sleep" "agentcore_memory_execution_ready" {
  count = local.agentcore_memory_count

  create_duration = "30s"
  depends_on      = [aws_iam_role_policy.agentcore_memory_execution]
}

# -----------------------------------------------------------------------------
# Memory + the single long-term strategy the gateway code expects.
# -----------------------------------------------------------------------------
resource "aws_bedrockagentcore_memory" "gateway_user_memory" {
  count = local.agentcore_memory_count

  name                      = local.agentcore_memory_name
  description               = "ADP ${var.environment} gateway-mediated shared user memory (#7210)"
  event_expiry_duration     = var.agentcore_memory_event_expiry_days
  memory_execution_role_arn = aws_iam_role.agentcore_memory_execution[0].arn

  tags = merge(local.common_tags, {
    Name    = local.agentcore_memory_name
    Service = "bedrock-agentcore"
    Purpose = "shared-user-memory"
  })

  depends_on = [time_sleep.agentcore_memory_execution_ready]
}

resource "aws_bedrockagentcore_memory_strategy" "gateway_user_memory" {
  count = local.agentcore_memory_count

  memory_id           = aws_bedrockagentcore_memory.gateway_user_memory[0].id
  name                = "adp_${var.environment}_owner_memory"
  description         = "One namespace per gateway-derived owner; actorId is the HMAC owner digest."
  type                = var.agentcore_memory_strategy_type
  namespace_templates = [local.agentcore_memory_namespace_owner]
}

# -----------------------------------------------------------------------------
# Gateway service role: exactly the data-plane calls the code makes, on this
# memory only. Sources (src/agentauth): agentcore_recall.py retrieve_memory_records;
# agentcore_processing.py / agentcore_management.py / agentcore_migration.py
# create_event; agentcore_management.py list_memory_records, get_memory_record,
# delete_memory_record. tests/infra/test_agentcore_memory_iac.py derives this set
# from the code and fails if the two drift. The statement reaches the role
# through the consolidated agents policy in gateway-role-policies.tf
# (aws_iam_policy.gateway_role["agents"]); it is empty while the switch is off.
# -----------------------------------------------------------------------------
locals {
  gateway_agentcore_memory_statements = local.agentcore_memory_count == 1 ? [
    {
      Sid    = "GatewayOwnedMemoryDataPlane"
      Effect = "Allow"
      Action = [
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:RetrieveMemoryRecords",
        "bedrock-agentcore:ListMemoryRecords",
        "bedrock-agentcore:GetMemoryRecord",
        "bedrock-agentcore:DeleteMemoryRecord"
      ]
      Resource = [aws_bedrockagentcore_memory.gateway_user_memory[0].arn]
    }
  ] : []
}

# -----------------------------------------------------------------------------
# Namespace key: 32 random bytes, hex-encoded (agentcore_recall() requires
# bytes.fromhex(value) to be >= 32 bytes). Lives only in Terraform state and the
# SecureString below; the renderers read it with --with-decryption and place it
# in the gateway Secret.
# -----------------------------------------------------------------------------
resource "random_bytes" "agentcore_namespace_key" {
  count  = local.agentcore_memory_count
  length = 32
}

resource "aws_ssm_parameter" "agentcore_namespace_key" {
  count = local.agentcore_memory_count

  name        = "/adp/${var.environment}/gateway/agentcore-namespace-key"
  description = "HMAC key deriving AgentCore owner namespaces (ADP_AGENTCORE_NAMESPACE_KEY); rotating it orphans every extracted record."
  type        = "SecureString"
  key_id      = aws_kms_key.secrets.arn
  value       = random_bytes.agentcore_namespace_key[0].hex
  tags        = local.common_tags
}

resource "aws_ssm_parameter" "agentcore_memory_id" {
  count = local.agentcore_memory_count

  name        = "/adp/${var.environment}/gateway/agentcore-memory-id"
  description = "AgentCore memory id for ADP_AGENTCORE_MEMORY_ID"
  type        = "String"
  value       = aws_bedrockagentcore_memory.gateway_user_memory[0].id
  tags        = local.common_tags
}

resource "aws_ssm_parameter" "agentcore_memory_strategy_id" {
  count = local.agentcore_memory_count

  name        = "/adp/${var.environment}/gateway/agentcore-memory-strategy-id"
  description = "AgentCore memory strategy id for ADP_AGENTCORE_MEMORY_STRATEGY_ID"
  type        = "String"
  value       = aws_bedrockagentcore_memory_strategy.gateway_user_memory[0].memory_strategy_id
  tags        = local.common_tags
}

resource "aws_ssm_parameter" "agentcore_memory_region" {
  count = local.agentcore_memory_count

  name        = "/adp/${var.environment}/gateway/agentcore-memory-region"
  description = "Region of the AgentCore memory for ADP_AGENTCORE_MEMORY_REGION; the gateway refuses a value that differs from its AWS_REGION"
  type        = "String"
  value       = var.aws_region
  tags        = local.common_tags
}

output "agentcore_memory_id" {
  description = "Gateway-owned AgentCore memory id (null when agentcore_memory_enabled is false)"
  value       = try(aws_bedrockagentcore_memory.gateway_user_memory[0].id, null)
}

output "agentcore_memory_arn" {
  description = "Gateway-owned AgentCore memory ARN (null when disabled)"
  value       = try(aws_bedrockagentcore_memory.gateway_user_memory[0].arn, null)
}

output "agentcore_memory_strategy_id" {
  description = "Strategy id the gateway must present as ADP_AGENTCORE_MEMORY_STRATEGY_ID (null when disabled)"
  value       = try(aws_bedrockagentcore_memory_strategy.gateway_user_memory[0].memory_strategy_id, null)
}

output "agentcore_namespace_key_parameter" {
  description = "SSM SecureString name holding ADP_AGENTCORE_NAMESPACE_KEY; read with --with-decryption and project into the gateway Secret (null when disabled)"
  value       = try(aws_ssm_parameter.agentcore_namespace_key[0].name, null)
}
