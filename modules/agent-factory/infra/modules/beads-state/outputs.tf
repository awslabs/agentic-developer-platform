output "dynamodb_table_name" {
  value = aws_dynamodb_table.beads_manifest.name
}

output "s3_bucket_name" {
  value = aws_s3_bucket.beads_state.id
}
