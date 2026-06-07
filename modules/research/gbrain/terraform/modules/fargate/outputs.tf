output "cluster_arn" {
  description = "ECS cluster ARN"
  value       = aws_ecs_cluster.gbrain.arn
}

output "cluster_name" {
  description = "ECS cluster name"
  value       = aws_ecs_cluster.gbrain.name
}

output "service_arn" {
  description = "ECS service ARN"
  value       = aws_ecs_service.mcp.id
}

output "service_endpoint" {
  description = "Service DNS name (task IPs — use with service discovery or direct IP)"
  value       = "${var.name_prefix}-mcp.internal"
}

output "task_definition_arn" {
  description = "ECS task definition ARN"
  value       = aws_ecs_task_definition.serve.arn
}
