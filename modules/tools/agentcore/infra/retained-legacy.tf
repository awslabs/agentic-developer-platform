locals {
  retained_browser     = var.retain_legacy_routes && local.browser_active == 1 ? 1 : 0
  retained_shared      = var.retain_legacy_routes ? local.active : 0
  retained_browser_api = var.retain_legacy_api_routes ? local.retained_browser : 0
  retained_shared_api  = var.retain_legacy_api_routes ? local.retained_shared : 0
  browser_route        = "${var.api_execution_arn}/${var.stage_name}/POST/tools/browser"
  interpreter_route    = "${var.api_execution_arn}/${var.stage_name}/POST/tools/code-interpreter"
  websearch_route      = "${var.api_execution_arn}/${var.stage_name}/POST/tools/websearch"
}

resource "aws_api_gateway_resource" "browser" {
  count       = local.retained_browser_api
  rest_api_id = var.rest_api_id
  parent_id   = var.tools_parent_resource_id
  path_part   = "browser"
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_api_gateway_method" "browser" {
  count         = local.retained_browser_api
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.browser[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_api_gateway_integration" "browser" {
  count                   = local.retained_browser_api
  rest_api_id             = var.rest_api_id
  resource_id             = aws_api_gateway_resource.browser[0].id
  http_method             = "POST"
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.browser[0].invoke_arn
  timeout_milliseconds    = 29000
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_lambda_permission" "browser" {
  count          = local.retained_browser
  statement_id   = "SharedApiBrowserRoute"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.browser[0].function_name
  principal      = "apigateway.amazonaws.com"
  source_arn     = local.browser_route
  source_account = var.aws_account_id
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_iam_role_policy" "browser_worker" {
  for_each = local.retained_browser == 1 ? var.worker_role_arns : toset([])
  name     = "${var.name}-browser-invoke"
  role     = element(reverse(split("/", each.value)), 0)
  policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Deny", Action = "execute-api:Invoke", Resource = local.browser_route }] })
  lifecycle { prevent_destroy = true }
}
resource "aws_cloudwatch_log_group" "browser" {
  count             = local.retained_browser
  name              = "/aws/lambda/${var.name}-browser"
  retention_in_days = 30
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_iam_role" "browser_gateway" {
  count = local.retained_browser
  name  = "${var.name}-browser-gateway"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" }
  }] })
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_iam_role_policy" "browser_gateway" {
  count = local.retained_browser
  name  = "browser-gateway"
  role  = aws_iam_role.browser_gateway[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.browser[0].arn}:*"
  }] })
  lifecycle { prevent_destroy = true }
}
resource "aws_lambda_function" "browser" {
  count         = local.retained_browser
  function_name = "${var.name}-browser"
  role          = aws_iam_role.browser_gateway[0].arn
  package_type  = "Image"
  image_uri     = var.image_uri
  image_config { command = ["agentcore_tools.legacy_retired.lambda_handler"] }
  timeout                        = 25
  memory_size                    = 512
  reserved_concurrent_executions = 10
  environment { variables = {} }
  lifecycle { prevent_destroy = true }
}

resource "aws_api_gateway_resource" "code_interpreter" {
  count       = local.retained_shared_api
  rest_api_id = var.rest_api_id
  parent_id   = var.tools_parent_resource_id
  path_part   = "code-interpreter"
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_api_gateway_method" "code_interpreter" {
  count         = local.retained_shared_api
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.code_interpreter[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_api_gateway_integration" "code_interpreter" {
  count                   = local.retained_shared_api
  rest_api_id             = var.rest_api_id
  resource_id             = aws_api_gateway_resource.code_interpreter[0].id
  http_method             = "POST"
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.service[0].invoke_arn
  timeout_milliseconds    = 29000
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_lambda_permission" "code_interpreter" {
  count          = local.retained_shared
  statement_id   = "SharedApiCodeInterpreterRoute"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.service[0].function_name
  principal      = "apigateway.amazonaws.com"
  source_arn     = local.interpreter_route
  source_account = var.aws_account_id
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_iam_role_policy" "code_interpreter_worker" {
  for_each = local.retained_shared == 1 ? var.worker_role_arns : toset([])
  name     = "${var.name}-code-interpreter-invoke"
  role     = element(reverse(split("/", each.value)), 0)
  policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Deny", Action = "execute-api:Invoke", Resource = local.interpreter_route }] })
  lifecycle { prevent_destroy = true }
}

resource "aws_api_gateway_resource" "websearch" {
  count       = local.retained_shared_api
  rest_api_id = var.rest_api_id
  parent_id   = var.tools_parent_resource_id
  path_part   = "websearch"
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_api_gateway_method" "websearch" {
  count         = local.retained_shared_api
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.websearch[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_api_gateway_integration" "websearch" {
  count                   = local.retained_shared_api
  rest_api_id             = var.rest_api_id
  resource_id             = aws_api_gateway_resource.websearch[0].id
  http_method             = "POST"
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.service[0].invoke_arn
  timeout_milliseconds    = 29000
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_lambda_permission" "websearch" {
  count          = local.retained_shared
  statement_id   = "SharedApiWebSearchRoute"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.service[0].function_name
  principal      = "apigateway.amazonaws.com"
  source_arn     = local.websearch_route
  source_account = var.aws_account_id
  lifecycle {
    prevent_destroy = true
    ignore_changes  = all
  }
}
resource "aws_iam_role_policy" "websearch_worker" {
  for_each = local.retained_shared == 1 ? var.worker_role_arns : toset([])
  name     = "${var.name}-websearch-invoke"
  role     = element(reverse(split("/", each.value)), 0)
  policy   = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Deny", Action = "execute-api:Invoke", Resource = local.websearch_route }] })
  lifecycle { prevent_destroy = true }
}
