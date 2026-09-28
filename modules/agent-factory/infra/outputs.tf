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
  value       = var.enable_github_apps ? module.secrets[0].secrets_prefix : "adp/${var.github_org}/gh-app-"
}

output "gateway_agent_role_arn" {
  description = "IAM role ARN for gateway agent worker pods (IRSA)"
  value       = aws_iam_role.gateway_agent.arn
}

output "gateway_agent_role_name" {
  description = "IAM role name for gateway agent worker pods"
  value       = aws_iam_role.gateway_agent.name
}

output "keda_operator_role_arn" {
  description = "IAM role ARN for the KEDA operator (owned by Phase 7, referenced here)"
  value       = data.aws_iam_role.keda_operator.arn
}

output "keda_operator_role_name" {
  description = "IAM role name for the KEDA operator (owned by Phase 7)"
  value       = data.aws_iam_role.keda_operator.name
}

output "public_cfn_bucket" {
  description = "S3 bucket name for public CloudFormation templates"
  value       = var.enable_public_cfn_bucket ? aws_s3_bucket.public_cfn[0].id : ""
}

output "public_cfn_bucket_url" {
  description = "Base URL for CloudFormation template downloads"
  value       = var.enable_public_cfn_bucket ? "https://${aws_s3_bucket.public_cfn[0].bucket}.s3.amazonaws.com" : ""
}
