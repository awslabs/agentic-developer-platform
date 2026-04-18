output "table_name" {
  description = "Name of the DynamoDB table"
  value       = aws_dynamodb_table.ingestion_state.name
}

output "table_arn" {
  description = "ARN of the DynamoDB table"
  value       = aws_dynamodb_table.ingestion_state.arn
}

output "readwrite_policy_arn" {
  description = "ARN of the IAM policy for read/write access"
  value       = aws_iam_policy.dynamodb_readwrite.arn
}
