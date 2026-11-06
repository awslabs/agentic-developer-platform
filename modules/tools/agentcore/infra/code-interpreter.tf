resource "aws_iam_role_policy" "code_interpreter_provider" {
  count = var.enabled && var.code_interpreter_arn != "" ? 1 : 0
  name  = "${var.name}-code-interpreter-provider"
  role  = aws_iam_role.service[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow",
    Action   = ["bedrock-agentcore:StartCodeInterpreterSession", "bedrock-agentcore:InvokeCodeInterpreter", "bedrock-agentcore:StopCodeInterpreterSession", "bedrock-agentcore:GetCodeInterpreterSession"],
    Resource = [var.code_interpreter_arn, "${var.code_interpreter_arn}/*"]
  }] })
}
