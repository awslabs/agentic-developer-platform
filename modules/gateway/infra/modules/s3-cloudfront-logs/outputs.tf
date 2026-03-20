output "bucket_domain_name" {
  description = "Domain name of the CloudFront logs bucket (for logging_config)"
  value       = aws_s3_bucket.cloudfront_logs.bucket_domain_name
}

output "bucket_arn" {
  description = "ARN of the CloudFront logs bucket"
  value       = aws_s3_bucket.cloudfront_logs.arn
}

output "bucket_id" {
  description = "ID of the CloudFront logs bucket"
  value       = aws_s3_bucket.cloudfront_logs.id
}
