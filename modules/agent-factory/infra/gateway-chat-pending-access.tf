resource "aws_iam_policy" "gateway_chat_pending_publish" {
  name = "adp-${var.environment}-policy-gateway-chat-pending"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ScopedChatPendingPublish"
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = [aws_sqs_queue.chat_agent_tasks_fifo.arn]
      }, {
      Sid      = "ScopedChatNotificationRecovery"
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
      Resource = [aws_dynamodb_table.chat_context.arn]
      Condition = {
        "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["chat-notifications"] }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "gateway_chat_pending_publish" {
  role       = "adp-${var.environment}-role-gateway-service"
  policy_arn = aws_iam_policy.gateway_chat_pending_publish.arn
}

resource "aws_ssm_parameter" "gateway_chat_pending_queue" {
  name        = "/adp/${var.environment}/agent-gateway/chat-input-queue-url"
  description = "Trusted gateway registered follow-up destination"
  type        = "String"
  value       = aws_sqs_queue.chat_agent_tasks_fifo.url
  tags        = { Component = "agent-gateway" }
}
