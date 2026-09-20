# Trusted gateway dispatch writes the Activity row atomically with authority
# and then sends to the existing queue. No worker principal receives these grants.
variable "gateway_authority_managed_policies" {
  description = "Use managed policies for new gateway authority grants when the existing role has exhausted its aggregate inline-policy quota. Enable through a reviewed environment rollout."
  type        = bool
  default     = false
}

locals {
  gateway_authorized_dispatch_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteAuthorizedInvocation"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = [aws_dynamodb_table.webhook_events.arn]
      },
      {
        Sid      = "PublishAuthorizedInvocation"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = [aws_sqs_queue.agent_submit.arn]
      },
      {
        Sid      = "DeliverOwnRunTask"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:ChangeMessageVisibility", "sqs:DeleteMessage"]
        Resource = [aws_sqs_queue.agent_submit.arn]
      },
      {
        Sid      = "WriteOwnRunArtifacts"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = ["${aws_s3_bucket.agent_run_logs.arn}/runs/*"]
      }
    ]
  })
}

resource "aws_iam_role_policy" "gateway_authorized_dispatch" {
  count  = local.agent_authority_provisioned && !var.gateway_authority_managed_policies ? 1 : 0
  name   = "adp-${var.environment}-policy-gateway-authorized-dispatch"
  role   = "adp-${var.environment}-role-gateway-service"
  policy = local.gateway_authorized_dispatch_policy
}

resource "aws_iam_policy" "gateway_authorized_dispatch" {
  count  = local.agent_authority_provisioned && var.gateway_authority_managed_policies ? 1 : 0
  name   = "adp-${var.environment}-policy-gateway-authorized-dispatch"
  policy = local.gateway_authorized_dispatch_policy
}

resource "aws_iam_role_policy_attachment" "gateway_authorized_dispatch" {
  count      = local.agent_authority_provisioned && var.gateway_authority_managed_policies ? 1 : 0
  role       = "adp-${var.environment}-role-gateway-service"
  policy_arn = aws_iam_policy.gateway_authorized_dispatch[0].arn
}
