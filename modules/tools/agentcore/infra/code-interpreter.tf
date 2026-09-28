resource "aws_api_gateway_resource" "code_interpreter" {
  count       = local.active
  rest_api_id = var.rest_api_id
  parent_id   = var.tools_parent_resource_id
  path_part   = "code-interpreter"
}
resource "aws_api_gateway_method" "code_interpreter" {
  count         = local.active
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.code_interpreter[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
}
resource "aws_api_gateway_integration" "code_interpreter" {
  count                   = local.active
  rest_api_id             = var.rest_api_id
  resource_id             = aws_api_gateway_resource.code_interpreter[0].id
  http_method             = aws_api_gateway_method.code_interpreter[0].http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.service[0].invoke_arn
  timeout_milliseconds    = 29000
}
resource "aws_lambda_permission" "code_interpreter" {
  count          = local.active
  statement_id   = "SharedApiCodeInterpreterRoute"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.service[0].function_name
  principal      = "apigateway.amazonaws.com"
  source_arn     = "${var.api_execution_arn}/${var.stage_name}/POST/tools/code-interpreter"
  source_account = var.aws_account_id
}
resource "aws_iam_role_policy" "code_interpreter_worker" {
  for_each = var.enabled ? var.worker_role_arns : toset([])
  name     = "${var.name}-code-interpreter-invoke"
  role     = element(reverse(split("/", each.value)), 0)
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow", Action = "execute-api:Invoke",
    Resource = "${var.api_execution_arn}/${var.stage_name}/POST/tools/code-interpreter"
  }] })
}
resource "aws_iam_role_policy" "code_interpreter_provider" {
  count = var.enabled && var.code_interpreter_arn != "" ? 1 : 0
  name  = "${var.name}-code-interpreter-provider"
  role  = aws_iam_role.service[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect   = "Allow",
    Action   = ["bedrock-agentcore:StartCodeInterpreterSession", "bedrock-agentcore:InvokeCodeInterpreter", "bedrock-agentcore:StopCodeInterpreterSession"],
    Resource = [var.code_interpreter_arn, "${var.code_interpreter_arn}/*"]
  }] })
}
