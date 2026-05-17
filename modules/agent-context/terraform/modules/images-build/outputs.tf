# =============================================================================
# Agent Context Images Build — Outputs
# =============================================================================

output "ecr_repository_urls" {
  description = "Map of image name to ECR repository URL"
  value       = { for k, v in aws_ecr_repository.agent_context_images : k => v.repository_url }
}

output "ecr_repository_arns" {
  description = "Map of image name to ECR repository ARN"
  value       = { for k, v in aws_ecr_repository.agent_context_images : k => v.arn }
}

output "codebuild_project_names" {
  description = "List of CodeBuild project names"
  value       = [for p in aws_codebuild_project.agent_context_images : p.name]
}
