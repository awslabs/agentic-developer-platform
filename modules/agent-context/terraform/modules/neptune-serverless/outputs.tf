output "cluster_endpoint" {
  description = "Neptune cluster endpoint"
  value       = aws_neptune_cluster.graphrag.endpoint
}

output "cluster_reader_endpoint" {
  description = "Neptune cluster reader endpoint"
  value       = aws_neptune_cluster.graphrag.reader_endpoint
}

output "cluster_port" {
  description = "Neptune cluster port"
  value       = aws_neptune_cluster.graphrag.port
}

output "cluster_resource_id" {
  description = "Neptune cluster resource ID (for IAM policies)"
  value       = aws_neptune_cluster.graphrag.cluster_resource_id
}

output "cluster_id" {
  description = "Neptune cluster identifier"
  value       = aws_neptune_cluster.graphrag.id
}

output "security_group_id" {
  description = "Security group ID for Neptune"
  value       = aws_security_group.neptune.id
}

output "access_role_arn" {
  description = "IAM role ARN for Neptune access (IRSA)"
  value       = aws_iam_role.neptune_access.arn
}

output "bulk_load_role_arn" {
  description = "IAM role ARN for Neptune Bulk Loader (S3 read access, attached to cluster)"
  value       = aws_iam_role.neptune_bulk_load.arn
}
