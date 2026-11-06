output "repository_name" { value = aws_ecr_repository.adapter.name }
output "repository_arn" { value = aws_ecr_repository.adapter.arn }
output "repository_url" { value = aws_ecr_repository.adapter.repository_url }
output "build_project_name" { value = aws_codebuild_project.build.name }
output "build_project_arn" { value = aws_codebuild_project.build.arn }
output "build_role_arn" { value = aws_iam_role.build.arn }
output "source_prefix" { value = local.source_prefix }
output "build_log_group" { value = aws_cloudwatch_log_group.build.name }
