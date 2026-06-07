output "project_name" {
  description = "CodeBuild project name (used by deploy.sh to trigger builds)"
  value       = aws_codebuild_project.gbrain_build.name
}

output "build_role_arn" {
  description = "IAM role ARN used by the CodeBuild project"
  value       = aws_iam_role.codebuild.arn
}
