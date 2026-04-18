output "collection_endpoint" {
  description = "OpenSearch Serverless collection endpoint"
  value       = aws_opensearchserverless_collection.graphrag.collection_endpoint
}

output "collection_id" {
  description = "OpenSearch Serverless collection ID"
  value       = aws_opensearchserverless_collection.graphrag.id
}

output "collection_arn" {
  description = "OpenSearch Serverless collection ARN"
  value       = aws_opensearchserverless_collection.graphrag.arn
}

output "dashboard_endpoint" {
  description = "OpenSearch Serverless dashboard endpoint"
  value       = aws_opensearchserverless_collection.graphrag.dashboard_endpoint
}

output "access_role_arn" {
  description = "IAM role ARN for OpenSearch access (IRSA)"
  value       = aws_iam_role.opensearch_access.arn
}
