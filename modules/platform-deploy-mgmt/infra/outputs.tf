output "evidence_bucket_name" {
  description = "S3 bucket for verification evidence"
  value       = aws_s3_bucket.evidence.id
}

output "evidence_bucket_arn" {
  description = "ARN of the evidence S3 bucket"
  value       = aws_s3_bucket.evidence.arn
}

output "deployments_table_name" {
  description = "DynamoDB table for deployment status tracking"
  value       = aws_dynamodb_table.deployments.name
}

output "deployments_table_arn" {
  description = "ARN of the deployments DynamoDB table"
  value       = aws_dynamodb_table.deployments.arn
}

output "verify_role_arn" {
  description = "IAM role ARN for the verify workflow (IRSA)"
  value       = aws_iam_role.verify_workflow.arn
}
