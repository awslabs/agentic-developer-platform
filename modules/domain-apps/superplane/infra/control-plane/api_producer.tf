# Explicit dedicated API identity. Existing controller/SkyPilot roles are unchanged.
variable "api_producer_role" {
  description = "Optional exact producer API target for the dedicated API role. Preserve on subsequent plans."
  type = object({
    api_id = string
    stage  = string
  })
  default = null
  validation {
    condition = var.api_producer_role == null ? true : (
      can(regex("^[a-z0-9]{10}$", var.api_producer_role.api_id)) &&
      can(regex("^[A-Za-z0-9_-]{1,128}$", var.api_producer_role.stage))
    )
    error_message = "The producer target requires an exact API ID and stage."
  }
}

locals {
  api_producer_name = "${local.name_prefix}-api-producer"
  api_producer_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = local.oidc_provider_arn }
      Condition = { StringEquals = {
        "${local.oidc_issuer}:sub" = "system:serviceaccount:${var.namespace}:superplane-api"
        "${local.oidc_issuer}:aud" = "sts.amazonaws.com"
      } }
    }]
  })
  api_producer_policy = var.api_producer_role == null ? null : jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "execute-api:Invoke"
      Resource = [for route in ["producer-readiness", "verify-run", "dispatch"] :
        "arn:aws:execute-api:${var.aws_region}:${var.account_id}:${var.api_producer_role.api_id}/${var.api_producer_role.stage}/POST/internal/v1/controller-execution/${route}"
      ]
    }]
  })
}

resource "aws_iam_role" "api_producer" {
  count               = var.api_producer_role == null ? 0 : 1
  name                = "${local.name_prefix}-api-producer"
  assume_role_policy  = local.api_producer_trust
  managed_policy_arns = []
  inline_policy {
    name   = local.api_producer_name
    policy = local.api_producer_policy
  }
}

output "api_producer_role" {
  description = "Applied identity, checked before the API ServiceAccount is changed."
  value = var.api_producer_role == null ? null : {
    arn     = aws_iam_role.api_producer[0].arn
    role_id = aws_iam_role.api_producer[0].unique_id
  }
}
