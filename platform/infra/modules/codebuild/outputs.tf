output "codebuild_role_arn" {
  description = <<-EOT
    ARN of the build role used by the agent-context image CodeBuild projects
    (modules/agent-context/terraform reads this via platform remote state).

    A18 (#5674): this was the shared AdministratorAccess role used by every
    project in this module. Each project in this module now has its OWN scoped
    role (see `project_role_arns`), and this output resolves to a role scoped to
    the agent-context image repositories. The output name is unchanged because
    it is a cross-state contract; the permissions behind it are not.
  EOT
  value       = aws_iam_role.agent_context_images.arn
}

output "codebuild_role_name" {
  description = "Name of the agent-context images build role (see codebuild_role_arn for the A18 scope change)"
  value       = aws_iam_role.agent_context_images.name
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
