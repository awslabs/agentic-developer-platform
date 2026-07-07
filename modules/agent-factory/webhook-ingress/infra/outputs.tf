# =============================================================================
# Outputs
# =============================================================================

output "api_endpoint" {
  description = "REST API Gateway invoke URL (includes stage)"
  value       = aws_api_gateway_stage.dev.invoke_url
}

output "api_id" {
  description = "REST API Gateway ID"
  value       = aws_api_gateway_rest_api.webhook.id
}

output "webhook_url" {
  description = "Full webhook URL (POST to this from GitHub)"
  value       = "${aws_api_gateway_stage.dev.invoke_url}/github"
}

output "waf_web_acl_arn" {
  description = "WAFv2 WebACL ARN associated with the webhook API stage"
  value       = aws_wafv2_web_acl.webhook.arn
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

output "github_app_id_secret_arn" {
  description = "Secrets Manager ARN for the ADP Agent Platform App ID"
  value       = aws_secretsmanager_secret.github_app_id.arn
}

output "github_app_key_secret_arn" {
  description = "Secrets Manager ARN for the ADP Agent Platform private key"
  value       = aws_secretsmanager_secret.github_app_key.arn
}

output "marker_signing_key_secret_arn" {
  description = "Secrets Manager ARN for the correlation marker HMAC signing key (S4)"
  value       = aws_secretsmanager_secret.marker_signing_key.arn
}

# =============================================================================
# SSM Parameters — consumed by CI integration tests
# =============================================================================

resource "aws_ssm_parameter" "webhook_endpoint" {
  name        = "/adp/${var.environment}/webhook-ingress/endpoint"
  description = "Webhook ingress API Gateway endpoint URL"
  type        = "String"
  value       = "${aws_api_gateway_stage.dev.invoke_url}/github"
}

resource "aws_ssm_parameter" "sqs_queue_url" {
  name        = "/adp/${var.environment}/webhook-ingress/sqs-queue-url"
  description = "SQS queue URL for webhook dispatch"
  type        = "String"
  value       = aws_sqs_queue.agent_submit.url
}

resource "aws_ssm_parameter" "webhook_secret_arn" {
  name        = "/adp/${var.environment}/webhook-ingress/webhook-secret-arn"
  description = "Secrets Manager ARN for the webhook HMAC secret"
  type        = "String"
  value       = aws_secretsmanager_secret.webhook_secret.arn
}

resource "aws_ssm_parameter" "webhook_events_table" {
  name        = "/adp/${var.environment}/webhook-ingress/webhook-events-table"
  description = "DynamoDB webhook-events table name"
  type        = "String"
  value       = aws_dynamodb_table.webhook_events.name
}

resource "aws_ssm_parameter" "tenant_registry_table" {
  name        = "/adp/${var.environment}/webhook-ingress/tenant-registry-table"
  description = "DynamoDB tenant-registry table name"
  type        = "String"
  value       = aws_dynamodb_table.tenant_registry.name
}

resource "aws_ssm_parameter" "rate_limits_table" {
  name        = "/adp/${var.environment}/webhook-ingress/rate-limits-table"
  description = "DynamoDB rate-limits table name"
  type        = "String"
  value       = aws_dynamodb_table.rate_limits.name
}

resource "aws_ssm_parameter" "marker_signing_key_arn" {
  name        = "/adp/${var.environment}/webhook-ingress/marker-signing-key-arn"
  description = "Secrets Manager ARN for the correlation marker signing key (S4)"
  type        = "String"
  value       = aws_secretsmanager_secret.marker_signing_key.arn
}

resource "aws_ssm_parameter" "identity_index_table" {
  name        = "/adp/${var.environment}/webhook-ingress/identity-index-table"
  description = "DynamoDB identity-index table name used by webhook Lambda (Phase B.1)"
  type        = "String"
  value       = var.identity_index_table_name
}

output "identity_index_table" {
  description = "DynamoDB identity-index table name consumed by webhook Lambda"
  value       = var.identity_index_table_name
}

# -----------------------------------------------------------------------------
# ScaledJob outputs
# -----------------------------------------------------------------------------

output "agent_scaledjob_namespace" {
  description = "Kubernetes namespace for hosted agent worker pods"
  value       = kubernetes_namespace.adp_agents.metadata[0].name
}

output "agent_scaledjob_role_arn" {
  description = "IAM role ARN for the agent ScaledJob service account (IRSA)"
  value       = aws_iam_role.agent_scaledjob.arn
}

output "agent_scaledjob_sa_name" {
  description = "Service account name for the agent ScaledJob pods"
  value       = kubernetes_service_account.agent_scaledjob_sa.metadata[0].name
}

# -----------------------------------------------------------------------------
# KEDA outputs (owned by this module as of #1052)
# -----------------------------------------------------------------------------

output "keda_operator_role_arn" {
  description = "IAM role ARN for the KEDA operator (IRSA, SQS read-only)"
  value       = aws_iam_role.keda_operator.arn
}

output "keda_operator_role_name" {
  description = "IAM role name for the KEDA operator"
  value       = aws_iam_role.keda_operator.name
}

# -----------------------------------------------------------------------------
# ADOT Collector outputs (Issue #1630)
# -----------------------------------------------------------------------------

output "otel_collector_endpoint" {
  description = "OTLP gRPC endpoint for the ADOT Collector (empty when disabled)"
  value       = var.enable_agent_otel ? "http://adot-collector.${kubernetes_namespace.adp_agents.metadata[0].name}.svc.cluster.local:4317" : ""
}

output "otel_collector_role_arn" {
  description = "IAM role ARN for the ADOT Collector service account (empty when disabled)"
  value       = var.enable_agent_otel ? aws_iam_role.otel_collector[0].arn : ""
}

# -----------------------------------------------------------------------------
# Agent Observability Dashboard (Issue #1680)
# -----------------------------------------------------------------------------

output "agent_observability_dashboard_name" {
  description = "CloudWatch dashboard name for agent business/application observability (empty when disabled)"
  value       = var.enable_agent_otel ? aws_cloudwatch_dashboard.agent_observability[0].dashboard_name : ""
}
