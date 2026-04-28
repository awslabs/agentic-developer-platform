# =============================================================================
# Outputs
# =============================================================================

# VPC
output "vpc_id" {
  description = "Threat Research VPC ID"
  value       = aws_vpc.threat_research.id
}

output "vpc_cidr" {
  description = "Threat Research VPC CIDR"
  value       = aws_vpc.threat_research.cidr_block
}

output "private_subnet_ids" {
  description = "Private subnet IDs"
  value       = aws_subnet.private[*].id
}

output "sandbox_subnet_id" {
  description = "Sandbox subnet ID"
  value       = aws_subnet.sandbox.id
}

# EC2
output "cape_host_instance_id" {
  description = "CAPE host EC2 instance ID"
  value       = aws_instance.cape_host.id
}

output "cape_host_private_ip" {
  description = "CAPE host private IP address"
  value       = aws_instance.cape_host.private_ip
}

output "cape_host_role_arn" {
  description = "CAPE host IAM role ARN"
  value       = aws_iam_role.cape_host.arn
}

# ALB
output "cape_alb_dns_name" {
  description = "Internal ALB DNS name for the CAPE API"
  value       = aws_lb.cape_internal.dns_name
}

output "cape_alb_arn" {
  description = "Internal ALB ARN"
  value       = aws_lb.cape_internal.arn
}

# Secrets
output "cape_api_token_secret_arn" {
  description = "Secrets Manager ARN for the CAPE API token"
  value       = aws_secretsmanager_secret.cape_api_token.arn
}

# Peering
output "vpc_peering_connection_id" {
  description = "VPC peering connection ID (empty if peering not configured)"
  value       = length(aws_vpc_peering_connection.adp_to_threat_research) > 0 ? aws_vpc_peering_connection.adp_to_threat_research[0].id : ""
}

# =============================================================================
# EKS Cluster (Issue #230)
# =============================================================================

output "cyber_cluster_name" {
  description = "Cyber EKS cluster name"
  value       = aws_eks_cluster.cyber.name
}

output "cyber_cluster_endpoint" {
  description = "Cyber EKS cluster API endpoint"
  value       = aws_eks_cluster.cyber.endpoint
}

output "cyber_cluster_oidc_issuer_url" {
  description = "Cyber EKS cluster OIDC issuer URL"
  value       = aws_eks_cluster.cyber.identity[0].oidc[0].issuer
}

output "cyber_oidc_provider_arn" {
  description = "Cyber EKS cluster OIDC provider ARN"
  value       = aws_iam_openid_connect_provider.cyber_cluster.arn
}

output "cyber_cluster_ca_certificate" {
  description = "Cyber EKS cluster CA certificate (base64)"
  value       = aws_eks_cluster.cyber.certificate_authority[0].data
}

# =============================================================================
# SQS Queues (Issue #230)
# =============================================================================

output "cyber_triage_tasks_queue_url" {
  description = "SQS FIFO queue URL for cyber triage tasks"
  value       = aws_sqs_queue.cyber_triage_tasks.url
}

output "cyber_triage_tasks_queue_arn" {
  description = "SQS FIFO queue ARN for cyber triage tasks"
  value       = aws_sqs_queue.cyber_triage_tasks.arn
}

output "cyber_static_tasks_queue_url" {
  description = "SQS FIFO queue URL for cyber static-analysis tasks"
  value       = aws_sqs_queue.cyber_static_tasks.url
}

output "cyber_static_tasks_queue_arn" {
  description = "SQS FIFO queue ARN for cyber static-analysis tasks"
  value       = aws_sqs_queue.cyber_static_tasks.arn
}

output "cyber_triage_responses_queue_url" {
  description = "SQS FIFO queue URL for cyber triage responses"
  value       = aws_sqs_queue.cyber_triage_responses.url
}

output "cyber_static_responses_queue_url" {
  description = "SQS FIFO queue URL for cyber static-analysis responses"
  value       = aws_sqs_queue.cyber_static_responses.url
}

# =============================================================================
# DynamoDB (Issue #230)
# =============================================================================

output "cyber_analysis_results_table_name" {
  description = "DynamoDB table name for cyber analysis results"
  value       = aws_dynamodb_table.cyber_analysis_results.name
}

output "cyber_analysis_results_table_arn" {
  description = "DynamoDB table ARN for cyber analysis results"
  value       = aws_dynamodb_table.cyber_analysis_results.arn
}

# =============================================================================
# Worker IRSA (Issue #230)
# =============================================================================

output "cyber_worker_role_arn" {
  description = "IAM role ARN for the cyber worker pods"
  value       = aws_iam_role.cyber_worker.arn
}

output "cyber_keda_operator_role_arn" {
  description = "IAM role ARN for the KEDA operator on the cyber cluster"
  value       = aws_iam_role.cyber_keda_operator.arn
}
