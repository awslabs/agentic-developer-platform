# Pure policy/enrollment contract. Creates no resource and grants nothing alone.
variable "environment" { type = string }
variable "account_id" { type = string }
variable "repository" { type = string }
variable "name_prefix" { type = string }
variable "enable_operator_source" {
  type     = bool
  default  = false
  nullable = false
}
variable "enable_paid_release" {
  type     = bool
  default  = false
  nullable = false
}
locals {
  superplane_source_prefix = "arn:aws:s3:::adp-terraform-state-${var.account_id}/superplane/releases/operator-source/${var.environment}"
  superplane_claim_prefix  = "arn:aws:s3:::adp-terraform-state-${var.account_id}/superplane/releases/paid-worker/dispatch"
}
output "publisher_identity_valid" {
  value = !(var.enable_operator_source || var.enable_paid_release) || (
    var.repository == "aws-e/adp" && var.name_prefix == "adp-${var.environment}" &&
    can(regex("^[a-z0-9]+(-[a-z0-9]+)*$", var.environment))
  )
}
output "project_names" {
  value = var.enable_paid_release ? ["adp-${var.environment}-superplane-paid-worker"] : []
}
output "ecr_repository_names" {
  value = var.enable_paid_release ? ["adp-superplane-paid-worker"] : []
}
output "policy_statements" {
  value = concat(
    [for _ in range(var.enable_operator_source ? 1 : 0) : {
      Sid      = "SuperplaneReviewedSource", Effect = "Allow",
      Action   = ["s3:PutObject", "s3:GetObject", "s3:GetObjectVersion"],
      Resource = [for kind in ["bundles", "consumers", "manifests"] : "${local.superplane_source_prefix}/*/${kind}/*"]
      }], [for _ in range(var.enable_operator_source ? 1 : 0) : {
      Sid      = "SuperplaneSourceBucketChecks", Effect = "Allow",
      Action   = ["s3:GetBucketVersioning", "s3:GetBucketPublicAccessBlock", "s3:GetBucketOwnershipControls", "s3:GetBucketLocation"],
      Resource = "arn:aws:s3:::adp-terraform-state-${var.account_id}"
      }], [for _ in range(var.enable_paid_release ? 1 : 0) : {
      Sid      = "SuperplanePaidDispatchEvidence", Effect = "Allow", Action = ["s3:PutObject"],
      Resource = [for name in ["claim.json", "child.json"] : "${local.superplane_claim_prefix}/*/${name}"]
      }], [for _ in range(var.enable_paid_release ? 1 : 0) : {
      Sid      = "SuperplanePaidRetentionChecks", Effect = "Allow",
      Action   = ["s3:GetBucketVersioning", "s3:GetLifecycleConfiguration"],
      Resource = "arn:aws:s3:::adp-terraform-state-${var.account_id}"
    }]
  )
}
