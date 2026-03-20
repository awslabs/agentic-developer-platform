# ECR Repository Outputs
output "repository_arn" {
  description = "Full ARN of the repository"
  value       = aws_ecr_repository.main.arn
}

output "repository_name" {
  description = "Name of the repository"
  value       = aws_ecr_repository.main.name
}

output "repository_url" {
  description = "URL of the repository"
  value       = aws_ecr_repository.main.repository_url
}

output "registry_id" {
  description = "Registry ID where the repository was created"
  value       = aws_ecr_repository.main.registry_id
}

# ECR Repository URI for different image tags
output "repository_uri_latest" {
  description = "Repository URI for latest tag"
  value       = "${aws_ecr_repository.main.repository_url}:latest"
}

output "repository_uri_dev" {
  description = "Repository URI for dev tag"
  value       = "${aws_ecr_repository.main.repository_url}:dev"
}

output "repository_uri_staging" {
  description = "Repository URI for staging tag"
  value       = "${aws_ecr_repository.main.repository_url}:staging"
}

output "repository_uri_prod" {
  description = "Repository URI for prod tag"
  value       = "${aws_ecr_repository.main.repository_url}:prod"
}

# ECR Lifecycle Policy
output "lifecycle_policy" {
  description = "The lifecycle policy document of the repository"
  value       = aws_ecr_lifecycle_policy.main.policy
}

# ECR Repository Policy (only exists when cross_account_arns is non-empty)
output "repository_policy" {
  description = "The repository policy document of the repository"
  value       = length(var.cross_account_arns) > 0 ? aws_ecr_repository_policy.main[0].policy : null
}

# Pull Through Cache Rules (conditional)
output "pull_through_cache_dockerhub" {
  description = "Docker Hub pull through cache rule"
  value = var.enable_pull_through_cache ? {
    ecr_repository_prefix = aws_ecr_pull_through_cache_rule.dockerhub[0].ecr_repository_prefix
    upstream_registry_url = aws_ecr_pull_through_cache_rule.dockerhub[0].upstream_registry_url
  } : null
}

output "pull_through_cache_public_ecr" {
  description = "Public ECR pull through cache rule"
  value = var.enable_pull_through_cache ? {
    ecr_repository_prefix = aws_ecr_pull_through_cache_rule.public_ecr[0].ecr_repository_prefix
    upstream_registry_url = aws_ecr_pull_through_cache_rule.public_ecr[0].upstream_registry_url
  } : null
}

# EventBridge Rule (conditional)
output "event_rule_arn" {
  description = "ARN of the EventBridge rule for ECR image pushes"
  value       = var.enable_event_notifications ? aws_cloudwatch_event_rule.ecr_push[0].arn : null
}

# CloudWatch Log Group
output "cloudwatch_log_group_name" {
  description = "Name of the CloudWatch log group for ECR"
  value       = aws_cloudwatch_log_group.ecr_logs.name
}

output "cloudwatch_log_group_arn" {
  description = "ARN of the CloudWatch log group for ECR"
  value       = aws_cloudwatch_log_group.ecr_logs.arn
}

# Docker commands for reference
output "docker_push_commands" {
  description = "Sample Docker commands for pushing images"
  value = {
    login = "aws ecr get-login-password --region ${data.aws_caller_identity.current.id} | docker login --username AWS --password-stdin ${aws_ecr_repository.main.repository_url}"
    build = "docker build -t ${aws_ecr_repository.main.name} ."
    tag   = "docker tag ${aws_ecr_repository.main.name}:latest ${aws_ecr_repository.main.repository_url}:latest"
    push  = "docker push ${aws_ecr_repository.main.repository_url}:latest"
  }
}