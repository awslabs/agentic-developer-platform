variable "registration_readback" {
  description = "Reviewed repository and existing administrator/group secret ARNs; null preserves identity-only setup."
  type = object({
    repository_id      = number
    secret_policy_arns = set(string)
  })
  default = null
  validation {
    condition = var.registration_readback == null ? true : (
      var.registration_readback.repository_id > 0 && floor(var.registration_readback.repository_id) == var.registration_readback.repository_id &&
      length(var.registration_readback.secret_policy_arns) == 3 &&
      alltrue([for arn in var.registration_readback.secret_policy_arns : can(regex(
        "^arn:aws:secretsmanager:${var.aws_region}:${var.account_id}:secret:adp/${var.environment}/(gitlab-admin-token|gitlab-runner/[0-9]+)-[A-Za-z0-9]{6}$", arn
      ))]) &&
      length([for arn in var.registration_readback.secret_policy_arns : arn if can(regex("/gitlab-admin-token-", arn))]) == 1
    )
    error_message = "Bind one repository, its existing admin secret and exactly two group-token secrets in this account/environment."
  }
}

locals {
  registration_evidence_bucket_arn = "arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}"
  registration_checks = [
    "registration-product-group", "registration-qualification-groups", "ca-root-verified",
    "group-token-scope", "runner-revocation", "instance-no-unreviewed-change",
  ]
  registration_evidence_arns = var.registration_readback == null ? [] : [
    for check in local.registration_checks : "${local.registration_evidence_bucket_arn}/gitlab-registration/repos/${var.registration_readback.repository_id}/runs/*/attempts/*/${check}.json"
  ]
}

# Import a coordinator-applied policy into this address; never replay a saved
# create plan. No registrar token access, trust, instance or gate changes here.
resource "aws_iam_role_policy" "registration_readback" {
  count = var.registration_readback == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-registration-readback"
  role  = aws_iam_role.deployment.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadReviewedSecretResourcePolicies", Effect = "Allow"
        Action   = ["secretsmanager:GetResourcePolicy"]
        Resource = sort(tolist(var.registration_readback.secret_policy_arns))
      },
      {
        Sid      = "ReadEvidenceBucketControls", Effect = "Allow"
        Action   = ["s3:GetBucketVersioning", "s3:GetBucketPublicAccessBlock"]
        Resource = [local.registration_evidence_bucket_arn]
      },
      {
        Sid      = "ReadVersionedRegistrationEvidence", Effect = "Allow", Action = "s3:GetObjectVersion"
        Resource = local.registration_evidence_arns
      },
      {
        Sid      = "WriteImmutableRegistrationEvidence", Effect = "Allow", Action = "s3:PutObject"
        Resource = local.registration_evidence_arns
        Condition = { StringEquals = {
          "s3:x-amz-server-side-encryption" = "AES256", "s3:if-none-match" = "*"
        } }
      },
      {
        Sid       = "PublishSignedDeliveryEvidence", Effect = "Allow", Action = "s3:PutObject"
        Resource  = ["${local.registration_evidence_bucket_arn}/deployment-evidence/v1/${var.registration_readback.repository_id}/*/*/delivery/delivery-evidence.json"]
        Condition = { StringEquals = { "s3:if-none-match" = "*" } }
      }
    ]
  })
}
