# =============================================================================
# Outputs
# =============================================================================

# VPC
output "vpc_id" {
  description = "Threat Research VPC ID"
  value       = aws_vpc.threat_research.id
}

output "vpc_cidr" {
  description = "Threat Research VPC CIDR"
  value       = aws_vpc.threat_research.cidr_block
}

output "private_subnet_ids" {
  description = "Private subnet IDs"
  value       = aws_subnet.private[*].id
}

output "sandbox_subnet_id" {
  description = "Sandbox subnet ID"
  value       = aws_subnet.sandbox.id
}

# EC2
output "cape_host_instance_id" {
  description = "CAPE host EC2 instance ID"
  value       = aws_instance.cape_host.id
}

output "cape_host_private_ip" {
  description = "CAPE host private IP address"
  value       = aws_instance.cape_host.private_ip
}

output "cape_host_role_arn" {
  description = "CAPE host IAM role ARN"
  value       = aws_iam_role.cape_host.arn
}

# ALB
output "cape_alb_dns_name" {
  description = "Internal ALB DNS name for the CAPE API"
  value       = aws_lb.cape_internal.dns_name
}

output "cape_alb_arn" {
  description = "Internal ALB ARN"
  value       = aws_lb.cape_internal.arn
}

# Secrets
output "cape_api_token_secret_arn" {
  description = "Secrets Manager ARN for the CAPE API token"
  value       = aws_secretsmanager_secret.cape_api_token.arn
}

# Peering
output "vpc_peering_connection_id" {
  description = "VPC peering connection ID (empty if peering not configured)"
  value       = length(aws_vpc_peering_connection.adp_to_threat_research) > 0 ? aws_vpc_peering_connection.adp_to_threat_research[0].id : ""
}
