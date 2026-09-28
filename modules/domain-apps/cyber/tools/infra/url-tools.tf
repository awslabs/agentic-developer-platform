resource "aws_iam_role_policy" "common_crawl" {
  count  = var.enabled && var.common_crawl_policy != "" ? 1 : 0
  name   = "${var.name}-common-crawl"
  role   = aws_iam_role.service[0].id
  policy = var.common_crawl_policy
}
resource "aws_api_gateway_resource" "common_crawl" {
  count       = local.active
  rest_api_id = var.rest_api_id
  parent_id   = aws_api_gateway_resource.cyber[0].id
  path_part   = "common-crawl"
}
resource "aws_api_gateway_method" "common_crawl" {
  count         = local.active
  rest_api_id   = var.rest_api_id
  resource_id   = aws_api_gateway_resource.common_crawl[0].id
  http_method   = "POST"
  authorization = "AWS_IAM"
}
resource "aws_api_gateway_integration" "common_crawl" {
  count                   = local.active
  rest_api_id             = var.rest_api_id
  resource_id             = aws_api_gateway_resource.common_crawl[0].id
  http_method             = "POST"
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.service[0].invoke_arn
  timeout_milliseconds    = 29000
  depends_on              = [aws_api_gateway_method.common_crawl]
}
resource "aws_lambda_permission" "common_crawl" {
  count          = local.active
  statement_id   = "SharedApiCommonCrawlRoute"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.service[0].function_name
  principal      = "apigateway.amazonaws.com"
  source_arn     = "${local.route_arn}/common-crawl"
  source_account = var.aws_account_id
}
resource "aws_iam_role_policy" "url_tools_worker" {
  for_each = var.enabled ? var.worker_role_arns : toset([])
  name     = "${var.name}-url-tools-invoke"
  role     = element(reverse(split("/", each.value)), 0)
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = "execute-api:Invoke", Resource = ["${local.route_arn}/common-crawl"]
  }] })
}
