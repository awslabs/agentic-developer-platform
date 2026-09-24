output "agent_context_project_role_arns" {
  description = "Map of agent-context image key to its dedicated build role ARN"
  value       = { for key, role in aws_iam_role.agent_context_image : key => role.arn }
}

output "project_role_arns" {
  description = "Map of logical project key to that project's dedicated build role ARN (A18, #5674 — no role is shared between projects)"
  value       = { for k, v in aws_iam_role.project : k => v.arn }
}

output "codebuild_boundary_arn" {
  description = "ARN of the permissions boundary attached to every build role (denies identity mutation, role assumption and build-input tampering)"
  value       = aws_iam_policy.codebuild_boundary.arn
}

output "project_names" {
  description = "Map of logical key to CodeBuild project name"
  value       = { for k, v in aws_codebuild_project.main : k => v.name }
}
