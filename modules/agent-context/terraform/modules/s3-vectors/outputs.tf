output "vector_bucket_name" {
  description = "Name of the S3 Vectors bucket"
  value       = local.vector_bucket_name
}

output "code_shard_index_names" {
  description = "Names of the code-shard vector indexes"
  value       = [for i in range(var.shard_count) : "code-shard-${i}"]
}

output "shard_count" {
  description = "Number of code-vector shards"
  value       = var.shard_count
}
