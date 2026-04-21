output "codebuild_role_arn" {
  description = "ARN of the shared CodeBuild IAM role"
  value       = aws_iam_role.codebuild.arn
}

output "codebuild_role_name" {
  description = "Name of the shared CodeBuild IAM role"
  value       = aws_iam_role.codebuild.name
}

output "project_names" {
  description = "Map of logical key to CodeBuild project name"
  value       = { for k, v in aws_codebuild_project.main : k => v.name }
}
