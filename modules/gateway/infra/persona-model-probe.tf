# Dedicated platform destination for SDK qualification. The probe pod keeps its
# gateway-only role; only the gateway can mint these Bedrock-only credentials.
variable "persona_model_probe_destination_enabled" {
  description = "Prepare a Bedrock-only platform role for bounded SDK qualification; does not register a destination or enable paid probes."
  type        = bool
  default     = false
}

variable "persona_model_probe_external_id" {
  description = "Dedicated platform-probe trust ID, also configured on the operator-owned probe destination. Never use a personal connection ID."
  type        = string
  sensitive   = true
  default     = null
  validation {
    condition     = var.persona_model_probe_external_id == null ? true : can(regex("^(platform-probe:[A-Za-z0-9_-]{32,128}|adp-platform:[a-f0-9-]{36})$", var.persona_model_probe_external_id))
    error_message = "Use a dedicated platform-probe trust ID or the server-issued adp-platform destination UUID."
  }
}

resource "aws_iam_role" "persona_model_probe_destination" {
  permissions_boundary = var.automation_permissions_boundary_arn
  count                = var.persona_model_probe_destination_enabled ? 1 : 0
  name                 = "ADP-Agent-${var.environment}-pmm-platform-probe"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = local.gateway_service_irsa_role_arn }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "sts:ExternalId" = var.persona_model_probe_external_id }
      }
      }, {
      Effect    = "Allow"
      Principal = { AWS = local.gateway_service_irsa_role_arn }
      Action    = "sts:TagSession"
    }]
  })
  lifecycle {
    precondition {
      condition     = var.persona_model_probe_external_id != null
      error_message = "An enabled platform probe destination requires its dedicated ExternalId."
    }
  }
}

resource "aws_iam_role_policy" "persona_model_probe_destination" {
  count = var.persona_model_probe_destination_enabled ? 1 : 0
  name  = "platform-claude-sdk-qualification"
  role  = aws_iam_role.persona_model_probe_destination[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
      Resource = [
        # Native Responses authorizes InvokeModel against the account project.
        "arn:${data.aws_partition.current.partition}:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/default",
        "arn:${data.aws_partition.current.partition}:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:${data.aws_partition.current.partition}:bedrock:us-east-1::foundation-model/openai.gpt-6-astra",
        "arn:${data.aws_partition.current.partition}:bedrock:us-east-2::foundation-model/openai.gpt-6-astra",
        "arn:${data.aws_partition.current.partition}:bedrock:us-west-2::foundation-model/openai.gpt-6-astra",
        "arn:${data.aws_partition.current.partition}:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.openai.gpt-6-astra",
        "arn:${data.aws_partition.current.partition}:bedrock:us-east-1::foundation-model/openai.gpt-6-sol",
        "arn:${data.aws_partition.current.partition}:bedrock:us-east-2::foundation-model/openai.gpt-6-sol",
        "arn:${data.aws_partition.current.partition}:bedrock:us-west-2::foundation-model/openai.gpt-6-sol",
        "arn:${data.aws_partition.current.partition}:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.openai.gpt-6-sol",
        "arn:${data.aws_partition.current.partition}:bedrock:us-east-1::foundation-model/openai.gpt-6-luna",
        "arn:${data.aws_partition.current.partition}:bedrock:us-east-2::foundation-model/openai.gpt-6-luna",
        "arn:${data.aws_partition.current.partition}:bedrock:us-west-2::foundation-model/openai.gpt-6-luna",
        "arn:${data.aws_partition.current.partition}:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/us.openai.gpt-6-luna",
        "arn:${data.aws_partition.current.partition}:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-*",
        "arn:${data.aws_partition.current.partition}:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/global.anthropic.claude-*",
      ]
    }]
  })
}

output "persona_model_probe_destination_role_arn" {
  description = "Bedrock-only role; registration and paid probe admission remain separate verified operations."
  value       = try(aws_iam_role.persona_model_probe_destination[0].arn, null)
}
