variable "reviewed_instance_update" {
  description = "Enable only after owner approval of the exact instance and saved plan. Null preserves the instance gate."
  type = object({
    instance_id     = string
    source_revision = string
    plan_sha256     = string
    approval_ref    = string
    expires_at      = string
  })
  default = null
  validation {
    condition = var.reviewed_instance_update == null ? true : (
      can(regex("^i-[0-9a-f]{8,17}$", var.reviewed_instance_update.instance_id)) &&
      can(regex("^[0-9a-f]{40}$", var.reviewed_instance_update.source_revision)) &&
      can(regex("^[0-9a-f]{64}$", var.reviewed_instance_update.plan_sha256)) &&
      length(var.reviewed_instance_update.approval_ref) > 0 && length(var.reviewed_instance_update.approval_ref) < 512 &&
      can(formatdate("YYYY-MM-DD'T'hh:mm:ssZ", var.reviewed_instance_update.expires_at))
    )
    error_message = "An instance update needs its exact instance, reviewed source/plan digests, attributable approval reference and explicit RFC3339 expiry."
  }
}

# AWS authorization context spells this attribute UserData, even when the
# API Attribute parameter uses userData. Keep this exact case-sensitive bound.
# IAM limits mutation to userData on this one instance. The protected workflow
# additionally checks the immutable plan/source and approval before Terraform.
# This does not authorize replacement, volume deletion, IAM edits or PassRole.
resource "aws_iam_role_policy" "reviewed_instance_update" {
  count = var.reviewed_instance_update == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-reviewed-instance-update"
  role  = aws_iam_role.deployment.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow", Action = ["ec2:StopInstances", "ec2:StartInstances"]
        Resource  = "arn:aws:ec2:${var.aws_region}:${var.account_id}:instance/${var.reviewed_instance_update.instance_id}"
        Condition = { DateLessThan = { "aws:CurrentTime" = var.reviewed_instance_update.expires_at } }
      },
      {
        Effect   = "Allow", Action = "ec2:ModifyInstanceAttribute"
        Resource = "arn:aws:ec2:${var.aws_region}:${var.account_id}:instance/${var.reviewed_instance_update.instance_id}"
        Condition = {
          StringEquals = { "ec2:Attribute" = "UserData" }
          DateLessThan = { "aws:CurrentTime" = var.reviewed_instance_update.expires_at }
        }
      },
    ]
  })
}
