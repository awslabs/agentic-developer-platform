output "secrets_prefix" {
  description = "Prefix for all GitHub App secrets (org-namespaced)."
  value       = local.secrets_prefix
}

output "app_id_secret_arns" {
  description = "Map of persona role -> App ID secret ARN"
  value       = { for k, s in data.aws_secretsmanager_secret.app_id : k => s.arn }
}

output "app_key_secret_arns" {
  description = "Map of persona role -> private key secret ARN"
  value       = { for k, s in data.aws_secretsmanager_secret.app_key : k => s.arn }
}
