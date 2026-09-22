# Exact operator-approved database identities for retained diagnostic workflows.
# A username is configuration; the workflow never fetches a master password.
variable "deployment_db_user_arns" {
  type    = list(string)
  default = []
  validation {
    condition     = alltrue([for arn in var.deployment_db_user_arns : can(regex("^arn:aws:rds-db:[a-z0-9-]+:[0-9]{12}:dbuser:[A-Za-z0-9-]+/[A-Za-z0-9_]+$", arn))])
    error_message = "Use exact database-resource/user ARNs without wildcards."
  }
}
resource "aws_iam_role_policy" "database_diagnostics" {
  count = length(var.deployment_db_user_arns) == 0 ? 0 : 1
  name  = "reviewed-database-diagnostics"
  role  = aws_iam_role.deployment.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["rds-db:connect"], Resource = var.deployment_db_user_arns
  }] })
}
resource "aws_iam_role_policy" "context_artifact_diagnostics" {
  name = "existing-context-artifact-diagnostics"
  role = aws_iam_role.deployment.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Action = ["s3:ListBucket"],
    Resource  = "arn:aws:s3:::agent-context-platform-data-${data.aws_caller_identity.current.account_id}",
    Condition = { StringLike = { "s3:prefix" = ["content/*"] } }
  }] })
}
