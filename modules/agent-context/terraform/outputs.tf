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

# --- S3 Storage (Mountpoint for Amazon S3) ---

output "bucket_name" {
  description = "S3 bucket name for platform data"
  value       = module.s3_files.bucket_name
}

output "bucket_arn" {
  description = "S3 bucket ARN for platform data"
  value       = module.s3_files.bucket_arn
}

output "s3_csi_role_arn" {
  description = "IAM role ARN for the Mountpoint S3 CSI driver"
  value       = module.s3_files.s3_csi_role_arn
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

# --- RDS (agent_context database on shared gateway instance) ---

output "rds_bootstrap_job_name" {
  description = "Name of the K8s Job that creates the agent_context DB + user"
  value       = module.rds_bootstrap.bootstrap_job_name
}

output "ac_db_name" {
  description = "Name of the agent_context database"
  value       = module.rds_bootstrap.ac_db_name
}

output "ac_db_username" {
  description = "PostgreSQL username for agent-context workloads"
  value       = module.rds_bootstrap.ac_db_username
}

output "ac_rds_host" {
  description = "RDS instance hostname (shared with gateway)"
  value       = local.rds_host
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
