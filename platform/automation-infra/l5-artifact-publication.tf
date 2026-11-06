# Reuse the private, versioned release-evidence bucket and protected gateway
# publisher. Package publication needs no worker, IAM or tenant-data authority.
variable "enable_l5_artifact_publication" {
  type        = bool
  default     = false
  description = "Permit the protected gateway publisher to publish and verify shared L5 artifacts in its existing deployment-evidence bucket."
}

locals {
  gateway_l5_release_objects = var.enable_l5_artifact_publication ? ["${local.gateway_observer_bucket}/l5/security/*"] : []
}

resource "aws_iam_role_policy" "l5_artifact_publication" {
  count = var.enable_l5_artifact_publication ? 1 : 0
  name  = "l5-artifact-publication"
  role  = try(aws_iam_role.gateway_backend[0].id, "")
  lifecycle {
    precondition {
      condition     = var.enable_gateway_backend_deployment
      error_message = "L5 artifact publication requires the existing protected gateway deployment profile."
    }
  }
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid      = "PublishAndVerifySharedL5Artifacts"
    Effect   = "Allow"
    Action   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"]
    Resource = local.gateway_l5_release_objects
  }] })
}

output "l5_artifact_publication_bucket" {
  value = var.enable_l5_artifact_publication ? "adp-${var.environment}-deployment-evidence-${local.gateway_backend_account}" : null
}
