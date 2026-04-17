# Map of repo name -> ARN
output "repository_arns" {
  description = "Map of repository name to ARN"
  value       = { for k, v in aws_ecr_repository.main : k => v.arn }
}

# Map of repo name -> URL
output "repository_urls" {
  description = "Map of repository name to URL"
  value       = { for k, v in aws_ecr_repository.main : k => v.repository_url }
}

output "repository_names" {
  description = "List of repository names"
  value       = [for r in aws_ecr_repository.main : r.name]
}

output "registry_id" {
  description = "Registry ID where the repositories were created"
  value       = data.aws_caller_identity.current.account_id
}
