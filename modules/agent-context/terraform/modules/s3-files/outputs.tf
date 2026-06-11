output "bucket_name" {
  description = "S3 bucket name"
  value       = aws_s3_bucket.platform_data.id
}

output "bucket_arn" {
  description = "S3 bucket ARN"
  value       = aws_s3_bucket.platform_data.arn
}

output "s3_csi_role_arn" {
  description = "IAM role ARN for Mountpoint S3 CSI driver"
  value       = aws_iam_role.s3_csi_controller.arn
}
