output "trail_arn" {
  description = "ARN of the CloudTrail trail"
  value       = aws_cloudtrail.main.arn
}

output "trail_name" {
  description = "Name of the CloudTrail trail"
  value       = aws_cloudtrail.main.name
}

output "s3_bucket_name" {
  description = "Name of the S3 bucket for CloudTrail logs"
  value       = aws_s3_bucket.cloudtrail.id
}

output "s3_bucket_arn" {
  description = "ARN of the S3 bucket for CloudTrail logs"
  value       = aws_s3_bucket.cloudtrail.arn
}

output "kms_key_arn" {
  description = "ARN of the KMS key for CloudTrail log encryption"
  value       = aws_kms_key.cloudtrail.arn
}

output "kms_key_id" {
  description = "ID of the KMS key for CloudTrail log encryption"
  value       = aws_kms_key.cloudtrail.key_id
}
