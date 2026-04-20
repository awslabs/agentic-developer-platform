# =============================================================================
# RDS Bootstrap Module — Outputs
# =============================================================================

output "bootstrap_job_name" {
  description = "Name of the Kubernetes Job that granted rds_iam"
  value       = kubernetes_job.grant_rds_iam.metadata[0].name
}

output "bootstrap_service_account" {
  description = "Service account used by the bootstrap Job"
  value       = kubernetes_service_account.rds_bootstrap.metadata[0].name
}

output "bootstrap_role_arn" {
  description = "IAM role ARN used by the bootstrap Job for Secrets Manager access"
  value       = aws_iam_role.rds_bootstrap.arn
}
