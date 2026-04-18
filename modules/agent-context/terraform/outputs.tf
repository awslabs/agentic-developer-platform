# =============================================================================
# Agent Context Platform — Outputs
# =============================================================================

# --- IAM ---

output "irsa_role_arn" {
  description = "IAM role ARN for the agent-context service account (IRSA)"
  value       = module.iam.role_arn
}

output "irsa_role_name" {
  description = "IAM role name for the agent-context service account"
  value       = module.iam.role_name
}

# --- S3 Files ---

output "file_system_id" {
  description = "EFS file system ID for the S3 Files mount"
  value       = module.s3_files.file_system_id
}

output "bucket_name" {
  description = "S3 bucket name"
  value       = module.s3_files.bucket_name
}

output "mount_target_ips" {
  description = "IP addresses of the EFS mount targets"
  value       = module.s3_files.mount_target_ips
}

output "csi_controller_role_arn" {
  description = "IAM role ARN for the EFS CSI controller"
  value       = module.s3_files.csi_controller_role_arn
}

output "csi_node_role_arn" {
  description = "IAM role ARN for the EFS CSI node daemonset"
  value       = module.s3_files.csi_node_role_arn
}

# --- GraphRAG ---

output "neptune_endpoint" {
  description = "Neptune Serverless cluster endpoint"
  value       = var.graphrag_enabled ? module.neptune_serverless[0].cluster_endpoint : ""
}

output "neptune_port" {
  description = "Neptune Serverless cluster port"
  value       = var.graphrag_enabled ? module.neptune_serverless[0].cluster_port : 0
}

output "neptune_access_role_arn" {
  description = "IAM role ARN for Neptune access"
  value       = var.graphrag_enabled ? module.neptune_serverless[0].access_role_arn : ""
}

output "opensearch_collection_endpoint" {
  description = "OpenSearch Serverless collection endpoint"
  value       = var.graphrag_enabled ? module.opensearch_serverless[0].collection_endpoint : ""
}

output "opensearch_access_role_arn" {
  description = "IAM role ARN for OpenSearch Serverless access"
  value       = var.graphrag_enabled ? module.opensearch_serverless[0].access_role_arn : ""
}

# --- SQS + DynamoDB ---

output "ingestion_queue_url" {
  description = "URL of the SQS ingestion queue"
  value       = module.sqs_ingestion.queue_url
}

output "ingestion_queue_arn" {
  description = "ARN of the SQS ingestion queue"
  value       = module.sqs_ingestion.queue_arn
}

output "ingestion_dlq_url" {
  description = "URL of the SQS ingestion dead letter queue"
  value       = module.sqs_ingestion.dlq_url
}

output "dynamodb_table_name" {
  description = "Name of the DynamoDB ingestion state table"
  value       = module.dynamodb_state.table_name
}

output "dynamodb_table_arn" {
  description = "ARN of the DynamoDB ingestion state table"
  value       = module.dynamodb_state.table_arn
}
