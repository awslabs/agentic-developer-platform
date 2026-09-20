# Dedicated platform destination for SDK qualification. The probe pod keeps its
# gateway-only role; only the gateway can mint these Bedrock-only credentials.
variable "persona_model_probe_destination_enabled" {
  description = "Prepare a Bedrock-only platform role for bounded SDK qualification; does not register a destination or enable paid probes."
  type        = bool
  default     = false
}

resource "aws_iam_role" "persona_model_probe_destination" {
  count = var.persona_model_probe_destination_enabled ? 1 : 0
  name  = "ADP-Agent-${var.environment}-pmm-platform-probe"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = local.gateway_service_irsa_role_arn }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
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
        "arn:${data.aws_partition.current.partition}:bedrock:*::foundation-model/anthropic.claude-*",
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
