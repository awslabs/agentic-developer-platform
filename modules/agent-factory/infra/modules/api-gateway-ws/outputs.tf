output "api_endpoint" { value = aws_apigatewayv2_api.ws.api_endpoint }
output "api_id" { value = aws_apigatewayv2_api.ws.id }
output "execution_arn" { value = aws_apigatewayv2_api.ws.execution_arn }
output "stage_invoke_url" { value = "${aws_apigatewayv2_api.ws.api_endpoint}/${var.stage_name}" }
