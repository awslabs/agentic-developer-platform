output "vector_bucket_name" {
  description = "Name of the S3 Vectors bucket"
  value       = aws_s3vectors_vector_bucket.code_vectors.name
}

output "code_shard_index_names" {
  description = "Names of the code-shard vector indexes"
  value       = [for idx in aws_s3vectors_index.code_shards : idx.index_name]
}

output "shard_count" {
  description = "Number of code-vector shards"
  value       = var.shard_count
}
