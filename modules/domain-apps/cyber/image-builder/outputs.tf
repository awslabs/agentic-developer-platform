# =============================================================================
# Outputs
# =============================================================================

output "assets_bucket_name" {
  description = "S3 bucket name for CAPE assets"
  value       = aws_s3_bucket.cape_assets.id
}

output "assets_bucket_arn" {
  description = "S3 bucket ARN for CAPE assets"
  value       = aws_s3_bucket.cape_assets.arn
}

output "builder_instance_id" {
  description = "Build host instance ID (empty if build_host_enabled=false)"
  value       = var.build_host_enabled ? aws_instance.builder[0].id : ""
}

output "builder_private_ip" {
  description = "Build host private IP (empty if build_host_enabled=false)"
  value       = var.build_host_enabled ? aws_instance.builder[0].private_ip : ""
}
