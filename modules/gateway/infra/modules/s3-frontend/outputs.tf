# S3 Bucket Outputs

output "bucket_id" {
  description = "ID of the S3 bucket"
  value       = aws_s3_bucket.frontend.id
}

output "bucket_arn" {
  description = "ARN of the S3 bucket"
  value       = aws_s3_bucket.frontend.arn
}

output "bucket_name" {
  description = "Name of the S3 bucket"
  value       = aws_s3_bucket.frontend.bucket
}

output "bucket_regional_domain_name" {
  description = "Regional domain name of the S3 bucket (for CloudFront origin)"
  value       = aws_s3_bucket.frontend.bucket_regional_domain_name
}

output "bucket_domain_name" {
  description = "Domain name of the S3 bucket"
  value       = aws_s3_bucket.frontend.bucket_domain_name
}
