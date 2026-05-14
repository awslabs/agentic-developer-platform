output "bucket_name" {
  description = "Name of the security scans S3 bucket"
  value       = aws_s3_bucket.security_scans.bucket
}

output "bucket_arn" {
  description = "ARN of the security scans S3 bucket"
  value       = aws_s3_bucket.security_scans.arn
}
