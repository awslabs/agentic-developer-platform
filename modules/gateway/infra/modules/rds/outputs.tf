# RDS Instance Outputs
output "db_instance_arn" {
  description = "The ARN of the RDS instance"
  value       = aws_db_instance.main.arn
}

output "db_instance_endpoint" {
  description = "The RDS instance endpoint"
  value       = aws_db_instance.main.endpoint
}

output "db_instance_hosted_zone_id" {
  description = "The canonical hosted zone ID of the DB instance"
  value       = aws_db_instance.main.hosted_zone_id
}

output "db_instance_id" {
  description = "The RDS instance ID"
  value       = aws_db_instance.main.id
}

output "db_instance_resource_id" {
  description = "The RDS Resource ID of this instance"
  value       = aws_db_instance.main.resource_id
}

output "db_instance_status" {
  description = "The RDS instance status"
  value       = aws_db_instance.main.status
}

output "db_instance_name" {
  description = "The database name"
  value       = aws_db_instance.main.db_name
}

output "db_instance_username" {
  description = "The master username for the database"
  value       = aws_db_instance.main.username
  sensitive   = true
}

output "db_instance_port" {
  description = "The database port"
  value       = aws_db_instance.main.port
}

# Database Connection Information
output "db_connection_string" {
  description = "Database connection string (without password - use IAM auth token)"
  value       = "postgresql://${aws_db_instance.main.username}@${aws_db_instance.main.endpoint}/${aws_db_instance.main.db_name}"
  sensitive   = true
}

# IAM Database Authentication Outputs
output "db_instance_address" {
  description = "The hostname of the RDS instance (for IAM auth token generation)"
  value       = aws_db_instance.main.address
}

output "iam_database_authentication_enabled" {
  description = "Whether IAM database authentication is enabled"
  value       = aws_db_instance.main.iam_database_authentication_enabled
}

# DB Subnet Group Outputs
output "db_subnet_group_name" {
  description = "The name of the DB subnet group"
  value       = aws_db_subnet_group.main.name
}

output "db_subnet_group_arn" {
  description = "The ARN of the DB subnet group"
  value       = aws_db_subnet_group.main.arn
}

# DB Parameter Group Outputs
output "db_parameter_group_name" {
  description = "The name of the DB parameter group"
  value       = aws_db_parameter_group.main.name
}

output "db_parameter_group_arn" {
  description = "The ARN of the DB parameter group"
  value       = aws_db_parameter_group.main.arn
}

# Read Replica Outputs (conditional)
output "db_read_replica_endpoint" {
  description = "The read replica endpoint"
  value       = length(aws_db_instance.read_replica) > 0 ? aws_db_instance.read_replica[0].endpoint : null
}

output "db_read_replica_arn" {
  description = "The ARN of the read replica"
  value       = length(aws_db_instance.read_replica) > 0 ? aws_db_instance.read_replica[0].arn : null
}

# Monitoring Outputs
output "enhanced_monitoring_role_arn" {
  description = "The ARN of the IAM role for enhanced monitoring"
  value       = aws_iam_role.rds_enhanced_monitoring.arn
}

# CloudWatch Logs
output "cloudwatch_log_group_name" {
  description = "The name of the CloudWatch log group"
  value       = aws_cloudwatch_log_group.rds_log_group.name
}

output "cloudwatch_log_group_arn" {
  description = "The ARN of the CloudWatch log group"
  value       = aws_cloudwatch_log_group.rds_log_group.arn
}