# Redis Replication Group Outputs
# Note: We now use replication_group for all environments (single-node and multi-node)
# to support IAM authentication, since aws_elasticache_cluster doesn't support user_group_ids

output "replication_group_id" {
  description = "ID of the ElastiCache replication group"
  value       = aws_elasticache_replication_group.main.replication_group_id
}

output "replication_group_arn" {
  description = "ARN of the ElastiCache replication group"
  value       = aws_elasticache_replication_group.main.arn
}

output "primary_endpoint_address" {
  description = "Address of the primary endpoint for the replication group"
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
}

output "reader_endpoint_address" {
  description = "Address of the endpoint for the reader node in the replication group"
  value       = aws_elasticache_replication_group.main.reader_endpoint_address
}

output "configuration_endpoint_address" {
  description = "Address of the replication group configuration endpoint"
  value       = aws_elasticache_replication_group.main.configuration_endpoint_address
}

# IAM Authentication Outputs
output "redis_iam_user_id" {
  description = "ID of the ElastiCache IAM user for IAM authentication"
  value       = aws_elasticache_user.iam_user.user_id
}

output "redis_iam_user_arn" {
  description = "ARN of the ElastiCache IAM user"
  value       = aws_elasticache_user.iam_user.arn
}

output "redis_user_group_id" {
  description = "ID of the ElastiCache user group"
  value       = aws_elasticache_user_group.main.user_group_id
}

# Common Outputs
output "cache_nodes" {
  description = "List of cache nodes with address and port"
  value = [
    {
      address = aws_elasticache_replication_group.main.primary_endpoint_address
      port    = var.port
    }
  ]
}

output "port" {
  description = "Redis port"
  value       = var.port
}

output "endpoint" {
  description = "Redis endpoint (address:port format)"
  value       = "${aws_elasticache_replication_group.main.primary_endpoint_address}:${var.port}"
}

# Subnet Group Outputs
output "subnet_group_name" {
  description = "Name of the ElastiCache subnet group"
  value       = aws_elasticache_subnet_group.main.name
}

# Parameter Group Outputs
output "parameter_group_name" {
  description = "Name of the ElastiCache parameter group"
  value       = aws_elasticache_parameter_group.main.name
}

# Monitoring Outputs
output "cpu_alarm_arn" {
  description = "ARN of the CPU utilization CloudWatch alarm"
  value       = var.enable_monitoring && length(aws_cloudwatch_metric_alarm.redis_cpu) > 0 ? aws_cloudwatch_metric_alarm.redis_cpu[0].arn : null
}

output "memory_alarm_arn" {
  description = "ARN of the memory utilization CloudWatch alarm"
  value       = var.enable_monitoring && length(aws_cloudwatch_metric_alarm.redis_memory) > 0 ? aws_cloudwatch_metric_alarm.redis_memory[0].arn : null
}

# Note: auth_token_secret_arn and auth_token_secret_name outputs have been removed
# as we now use IAM authentication instead of password-based AUTH tokens
