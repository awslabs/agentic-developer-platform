output "bucket_name" {
  description = "S3 bucket name for brain repository"
  value       = aws_s3_bucket.brain_repo.id
}

output "bucket_arn" {
  description = "S3 bucket ARN"
  value       = aws_s3_bucket.brain_repo.arn
}

output "ecr_repo_url" {
  description = "ECR repository URL"
  value       = aws_ecr_repository.gbrain.repository_url
}

output "ecr_repo_arn" {
  description = "ECR repository ARN"
  value       = aws_ecr_repository.gbrain.arn
}
