# =============================================================================
# GitLab CE Infrastructure — Outputs
# =============================================================================

output "instance_id" {
  description = "EC2 instance ID of the GitLab CE server"
  value       = aws_instance.gitlab.id
}

output "instance_private_ip" {
  description = "Private IP address of the GitLab instance"
  value       = aws_instance.gitlab.private_ip
}

output "alb_dns_name" {
  description = "DNS name of the internal ALB"
  value       = aws_lb.gitlab.dns_name
}

output "alb_arn" {
  description = "ARN of the internal ALB"
  value       = aws_lb.gitlab.arn
}

output "gitlab_url" {
  description = "GitLab URL (FQDN via Route53)"
  value       = "https://${var.gitlab_domain}"
}

output "instance_security_group_id" {
  description = "Security group ID attached to the GitLab instance"
  value       = aws_security_group.instance.id
}

output "alb_security_group_id" {
  description = "Security group ID attached to the GitLab ALB"
  value       = aws_security_group.alb.id
}

output "iam_role_arn" {
  description = "ARN of the GitLab instance IAM role"
  value       = aws_iam_role.gitlab.arn
}

output "iam_instance_profile_name" {
  description = "Name of the GitLab instance profile"
  value       = aws_iam_instance_profile.gitlab.name
}
