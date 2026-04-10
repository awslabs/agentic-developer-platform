output "runner_role_arn" {
  description = "IAM role ARN for GitHub Actions runner pods"
  value       = module.runner_iam.runner_role_arn
}

output "beads_dynamodb_table" {
  description = "DynamoDB table name for beads issue tracking"
  value       = module.beads_state.dynamodb_table_name
}

output "beads_s3_bucket" {
  description = "S3 bucket name for beads state"
  value       = module.beads_state.s3_bucket_name
}

output "secrets_prefix" {
  description = "Secrets Manager prefix for GitHub App credentials"
  value       = module.secrets.secrets_prefix
}
