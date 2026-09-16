output "enabled" {
  value = var.enabled
}

output "region" {
  value = local.region
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.invocations.name
}

output "bucket_name" {
  value = aws_s3_bucket.logs.id
}

output "key_arn" {
  value = aws_kms_key.logs.arn
}
