output "file_system_id" {
  description = "EFS file system ID"
  value       = aws_efs_file_system.platform_data.id
}

output "bucket_name" {
  description = "S3 bucket name"
  value       = aws_s3_bucket.platform_data.id
}

output "bucket_arn" {
  description = "S3 bucket ARN"
  value       = aws_s3_bucket.platform_data.arn
}

output "mount_target_ips" {
  description = "IP addresses of the EFS mount targets"
  value       = aws_efs_mount_target.platform_data[*].ip_address
}

output "mount_target_ids" {
  description = "IDs of the EFS mount targets"
  value       = aws_efs_mount_target.platform_data[*].id
}

output "security_group_id" {
  description = "Security group ID for EFS mount targets"
  value       = aws_security_group.efs_mount.id
}

output "csi_controller_role_arn" {
  description = "IAM role ARN for EFS CSI controller"
  value       = aws_iam_role.efs_csi_controller.arn
}

output "csi_node_role_arn" {
  description = "IAM role ARN for EFS CSI node"
  value       = aws_iam_role.efs_csi_node.arn
}
