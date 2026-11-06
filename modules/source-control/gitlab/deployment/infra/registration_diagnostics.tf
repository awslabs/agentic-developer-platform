variable "registration_diagnostics_repository_id" {
  description = "Optional exact repository whose protected registration failures are retained privately. Null grants no diagnostic writes."
  type        = number
  default     = null
  validation {
    condition     = var.registration_diagnostics_repository_id == null ? true : var.registration_diagnostics_repository_id > 0 && floor(var.registration_diagnostics_repository_id) == var.registration_diagnostics_repository_id
    error_message = "Bind one positive repository ID."
  }
}

resource "aws_iam_role_policy" "registration_diagnostics" {
  count = var.registration_diagnostics_repository_id == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-registration-diagnostics"
  role  = aws_iam_role.deployment.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:PutObject"
      Resource = "arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}/gitlab-registration-diagnostics/repos/${var.registration_diagnostics_repository_id}/runs/*/attempts/*/terraform-failure.json"
      Condition = { StringEquals = {
        "s3:x-amz-server-side-encryption" = "AES256"
        "s3:if-none-match"                = "*"
      } }
    }]
  })
}
