# =============================================================================
# Outputs
# =============================================================================

output "api_endpoint" {
  description = "HTTP API Gateway endpoint URL"
  value       = aws_apigatewayv2_api.webhook.api_endpoint
}

output "api_id" {
  description = "HTTP API Gateway ID"
  value       = aws_apigatewayv2_api.webhook.id
}

output "webhook_url" {
  description = "Full webhook URL (POST to this from GitHub)"
  value       = "${aws_apigatewayv2_api.webhook.api_endpoint}/github"
}

output "lambda_function_name" {
  description = "GitHub webhook Lambda function name"
  value       = aws_lambda_function.github_webhook.function_name
}

output "lambda_function_arn" {
  description = "GitHub webhook Lambda function ARN"
  value       = aws_lambda_function.github_webhook.arn
}

output "sqs_queue_url" {
  description = "SQS FIFO queue URL for agent submissions"
  value       = aws_sqs_queue.agent_submit.url
}

output "sqs_queue_arn" {
  description = "SQS FIFO queue ARN"
  value       = aws_sqs_queue.agent_submit.arn
}

output "sqs_dlq_url" {
  description = "SQS dead-letter queue URL"
  value       = aws_sqs_queue.agent_submit_dlq.url
}

output "tenant_registry_table" {
  description = "DynamoDB tenant-registry table name"
  value       = aws_dynamodb_table.tenant_registry.name
}

output "webhook_events_table" {
  description = "DynamoDB webhook-events table name"
  value       = aws_dynamodb_table.webhook_events.name
}

output "rate_limits_table" {
  description = "DynamoDB rate-limits table name"
  value       = aws_dynamodb_table.rate_limits.name
}

output "webhook_secret_arn" {
  description = "Secrets Manager ARN for the webhook HMAC secret"
  value       = aws_secretsmanager_secret.webhook_secret.arn
}

output "waf_web_acl_arn" {
  description = "WAF WebACL ARN"
  value       = aws_wafv2_web_acl.webhook.arn
}
