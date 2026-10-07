resource "aws_iam_policy" "gateway_chat_completion" {
  name = "adp-${var.environment}-policy-gateway-chat-completion"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ScopedChatCompletionUpdate"
      Effect   = "Allow"
      Action   = ["dynamodb:UpdateItem", "dynamodb:ConditionCheckItem"]
      Resource = [module.gateway_sessions.table_arn]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "gateway_chat_completion" {
  role       = "adp-${var.environment}-role-gateway-service"
  policy_arn = aws_iam_policy.gateway_chat_completion.arn
}
