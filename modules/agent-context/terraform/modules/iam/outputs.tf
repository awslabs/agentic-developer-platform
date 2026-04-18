output "role_arn" {
  description = "IAM role ARN for the agent-context service account"
  value       = aws_iam_role.agent_context.arn
}

output "role_name" {
  description = "IAM role name for the agent-context service account"
  value       = aws_iam_role.agent_context.name
}
