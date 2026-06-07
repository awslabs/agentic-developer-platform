output "endpoint" {
  description = "RDS instance endpoint (host:port)"
  value       = aws_db_instance.gbrain.endpoint
}

output "address" {
  description = "RDS instance address (host only)"
  value       = aws_db_instance.gbrain.address
}

output "port" {
  description = "RDS instance port"
  value       = aws_db_instance.gbrain.port
}

output "credentials_secret_arn" {
  description = "Secrets Manager ARN for database credentials"
  value       = aws_secretsmanager_secret.db_credentials.arn
}
