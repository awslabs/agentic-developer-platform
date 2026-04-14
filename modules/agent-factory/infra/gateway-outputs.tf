output "gateway_ws_endpoint" {
  description = "WebSocket API Gateway endpoint"
  value       = module.gateway_apigw.stage_invoke_url
}

output "gateway_input_queue_url" {
  description = "Gateway input SQS queue URL"
  value       = module.gateway_sqs.input_queue_url
}

output "gateway_response_queue_url" {
  description = "Gateway response SQS queue URL"
  value       = module.gateway_sqs.response_queue_url
}

output "gateway_sessions_table" {
  description = "Gateway DynamoDB sessions table name"
  value       = module.gateway_sessions.table_name
}
