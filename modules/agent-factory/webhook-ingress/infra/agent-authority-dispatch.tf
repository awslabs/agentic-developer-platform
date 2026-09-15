# Trusted gateway dispatch writes the Activity row atomically with authority
# and then sends to the existing queue. No worker principal receives these grants.
resource "aws_iam_role_policy" "gateway_authorized_dispatch" {
  count = local.agent_authority_provisioned ? 1 : 0
  name  = "adp-${var.environment}-policy-gateway-authorized-dispatch"
  role  = "adp-${var.environment}-role-gateway-service"
  policy = jsonencode({
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
      }
    ]
  })
}
