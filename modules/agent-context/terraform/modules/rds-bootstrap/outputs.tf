# =============================================================================
# Agent-Context RDS Bootstrap Module — Outputs
# =============================================================================

output "bootstrap_job_name" {
  description = "Name of the Kubernetes bootstrap Job"
  value       = kubernetes_job.ac_bootstrap.metadata[0].name
}

output "service_account_name" {
  description = "Name of the IRSA service account for the bootstrap Job"
  value       = kubernetes_service_account.ac_rds_bootstrap.metadata[0].name
}

output "iam_role_arn" {
  description = "ARN of the IAM role for the bootstrap Job"
  value       = aws_iam_role.ac_rds_bootstrap.arn
}

output "ac_db_name" {
  description = "Name of the agent_context database created by this module"
  value       = var.ac_db_name
}

output "ac_db_username" {
  description = "PostgreSQL username for agent-context workloads"
  value       = var.ac_db_username
}
