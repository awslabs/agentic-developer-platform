variable "registration_context_repository_id" {
  description = "Repository whose protected registration workflow publishes signed provider context; null grants nothing."
  type        = number
  default     = null
  validation {
    condition     = var.registration_context_repository_id == null ? true : var.registration_context_repository_id > 0 && floor(var.registration_context_repository_id) == var.registration_context_repository_id
    error_message = "Bind one exact numeric repository ID."
  }
}

resource "aws_iam_role_policy" "registration_context" {
  count = var.registration_context_repository_id == null ? 0 : 1
  name  = "adp-${var.environment}-gitlab-registration-context"
  role  = aws_iam_role.deployment.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "PublishImmutableRegistrationContext", Effect = "Allow", Action = "s3:PutObject"
      Resource  = "arn:aws:s3:::adp-${var.environment}-deployment-evidence-${var.account_id}/deployment-evidence/v1/${var.registration_context_repository_id}/*/*/context/gitlab-infra-apply.yml.json"
      Condition = { StringEquals = { "s3:if-none-match" = "*" } }
    }]
  })
}
