# =============================================================================
# Outputs for S3 Chat Logs Module (Issue #143)
# =============================================================================

output "bucket_name" {
  description = "Name of the chat logs S3 bucket"
  value       = aws_s3_bucket.chat_logs.bucket
}

output "bucket_arn" {
  description = "ARN of the chat logs S3 bucket"
  value       = aws_s3_bucket.chat_logs.arn
}

output "bucket_id" {
  description = "ID of the chat logs S3 bucket"
  value       = aws_s3_bucket.chat_logs.id
}

output "bucket_regional_domain_name" {
  description = "Regional domain name of the chat logs S3 bucket"
  value       = aws_s3_bucket.chat_logs.bucket_regional_domain_name
}
