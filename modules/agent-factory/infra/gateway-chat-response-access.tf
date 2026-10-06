resource "aws_iam_policy" "gateway_chat_response_publish" {
  name = "adp-${var.environment}-policy-gateway-chat-response"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ScopedChatResponsesPublish"
      Effect   = "Allow"
      Action   = ["sqs:SendMessage"]
      Resource = [module.gateway_sqs.response_queue_arn]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "gateway_chat_response_publish" {
  role       = "adp-${var.environment}-role-gateway-service"
  policy_arn = aws_iam_policy.gateway_chat_response_publish.arn
}

resource "aws_ssm_parameter" "gateway_chat_response_queue" {
  name        = "/adp/${var.environment}/agent-gateway/response-queue-url"
  description = "Trusted gateway response relay destination; never mounted into model sandboxes"
  type        = "String"
  value       = module.gateway_sqs.response_queue_url
  tags        = { Component = "agent-gateway" }
}
