resource "aws_apigatewayv2_api" "ws" {
  name                       = "${var.name_prefix}-gateway-ws"
  protocol_type              = "WEBSOCKET"
  route_selection_expression = "$request.body.action"
  tags                       = var.tags
}

resource "aws_apigatewayv2_integration" "ingest" {
  api_id             = aws_apigatewayv2_api.ws.id
  integration_type   = "AWS_PROXY"
  integration_uri    = var.ingest_lambda_arn
  integration_method = "POST"
}

# $connect — with authorizer from gateway module (if available)
resource "aws_apigatewayv2_route" "connect" {
  api_id    = aws_apigatewayv2_api.ws.id
  route_key = "$connect"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"

  authorization_type = var.authorizer_lambda_invoke_arn != "" ? "CUSTOM" : "NONE"
  authorizer_id      = var.authorizer_lambda_invoke_arn != "" ? aws_apigatewayv2_authorizer.gateway[0].id : null
}

# Reuse the gateway module's existing Lambda authorizer
resource "aws_apigatewayv2_authorizer" "gateway" {
  count = var.authorizer_lambda_invoke_arn != "" ? 1 : 0

  api_id                            = aws_apigatewayv2_api.ws.id
  authorizer_type                   = "REQUEST"
  authorizer_uri                    = var.authorizer_lambda_invoke_arn
  name                              = "gateway-cognito-jwt"
  identity_sources                  = ["route.request.querystring.token"]
  authorizer_result_ttl_in_seconds  = 300
}

# Allow this WebSocket API to invoke the gateway's authorizer Lambda
resource "aws_lambda_permission" "authorizer_invoke" {
  count = var.authorizer_lambda_invoke_arn != "" ? 1 : 0

  statement_id  = "AllowAgentGatewayWSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = var.authorizer_lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.ws.execution_arn}/*"
}

resource "aws_apigatewayv2_route" "disconnect" {
  api_id    = aws_apigatewayv2_api.ws.id
  route_key = "$disconnect"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"
}

resource "aws_apigatewayv2_route" "send_message" {
  api_id    = aws_apigatewayv2_api.ws.id
  route_key = "sendMessage"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"
}

resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.ws.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.ingest.id}"
}

resource "aws_apigatewayv2_stage" "ws" {
  api_id      = aws_apigatewayv2_api.ws.id
  name        = var.stage_name
  auto_deploy = true

  default_route_settings {
    throttling_burst_limit = 100
    throttling_rate_limit  = 50
  }

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.ws.arn
    format = jsonencode({
      requestId    = "$context.requestId"
      routeKey     = "$context.routeKey"
      status       = "$context.status"
      connectionId = "$context.connectionId"
      authorizer   = "$context.authorizer.error"
    })
  }

  tags = var.tags
}

# Lambda permissions
resource "aws_lambda_permission" "ws_connect" {
  statement_id  = "AllowWSConnect"
  action        = "lambda:InvokeFunction"
  function_name = var.ingest_lambda_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.ws.execution_arn}/*/$connect"
}

resource "aws_lambda_permission" "ws_disconnect" {
  statement_id  = "AllowWSDisconnect"
  action        = "lambda:InvokeFunction"
  function_name = var.ingest_lambda_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.ws.execution_arn}/*/$disconnect"
}

resource "aws_lambda_permission" "ws_send_message" {
  statement_id  = "AllowWSSendMessage"
  action        = "lambda:InvokeFunction"
  function_name = var.ingest_lambda_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.ws.execution_arn}/*/sendMessage"
}

resource "aws_lambda_permission" "ws_default" {
  statement_id  = "AllowWSDefault"
  action        = "lambda:InvokeFunction"
  function_name = var.ingest_lambda_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.ws.execution_arn}/*/$default"
}

resource "aws_cloudwatch_log_group" "ws" {
  name              = "/aws/apigateway/${var.name_prefix}-gateway-ws"
  retention_in_days = 30
  tags              = var.tags
}
