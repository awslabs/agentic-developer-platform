locals {
  authority_resources = var.agent_authority_resources != null ? var.agent_authority_resources : {
    webhook_events_table_name  = var.webhook_events_table_name
    webhook_events_kms_key_arn = var.webhook_events_kms_key_arn
  }
}

resource "aws_iam_role_policy" "agent_authority" {
  count = var.agent_authority_prepared || var.agent_authority_enabled ? 1 : 0
  name  = "${var.name_prefix}-orchestration-agent-authority"
  role  = aws_iam_role.tick.id
  lifecycle {
    precondition {
      condition     = local.authority_resources.webhook_events_table_name != "" && local.authority_resources.webhook_events_kms_key_arn != ""
      error_message = "Tick authority preparation requires the webhook table and its encryption key."
    }
  }
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"]
        Resource = ["arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.name_prefix}-agent-authority"]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = ["arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${local.authority_resources.webhook_events_table_name}"]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource = [local.authority_resources.webhook_events_kms_key_arn]
      }
    ]
  })
}
