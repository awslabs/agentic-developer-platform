# Operator-owned ceiling for the existing webhook execution role. It grants
# nothing by itself: the role's existing resource-scoped policies still apply.
# Code publishers cannot change this policy or the role's boundary attachment.
variable "enable_webhook_execution_boundary" {
  type    = bool
  default = false
}

locals {
  webhook_execution_actions = [
    "cloudwatch:PutMetricData",
    "dynamodb:ConditionCheckItem", "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem",
    "execute-api:Invoke",
    "kms:Decrypt", "kms:DescribeKey", "kms:GenerateDataKey", "kms:GenerateDataKeyWithoutPlaintext",
    "logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents",
    "secretsmanager:CreateSecret", "secretsmanager:GetSecretValue", "secretsmanager:TagResource",
    "sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:SendMessage", "ssm:GetParameter",
  ]
}

resource "aws_iam_policy" "webhook_execution_boundary" {
  count = var.enable_webhook_execution_boundary ? 1 : 0
  name  = "${var.name_prefix}-webhook-execution-boundary"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "ExistingWebhookDataAPIs", Effect = "Allow", Action = local.webhook_execution_actions, Resource = "*" },
      { Sid = "DenyOutsideWebhookDataAPIs", Effect = "Deny", NotAction = local.webhook_execution_actions, Resource = "*" },
      {
        Sid         = "TenantAppSecretCreationOnly"
        Effect      = "Deny"
        Action      = ["secretsmanager:CreateSecret", "secretsmanager:TagResource"]
        NotResource = "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:adp/${var.environment}/tenants/*"
      },
    ]
  })
}

output "webhook_execution_boundary_arn" {
  value = var.enable_webhook_execution_boundary ? aws_iam_policy.webhook_execution_boundary[0].arn : ""
}
