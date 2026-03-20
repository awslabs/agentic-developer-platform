# Load Balancer Outputs
output "load_balancer_arn" {
  description = "ARN of the load balancer"
  value       = aws_lb.main.arn
}

output "load_balancer_arn_suffix" {
  description = "ARN suffix for use with CloudWatch metrics"
  value       = aws_lb.main.arn_suffix
}

output "load_balancer_dns_name" {
  description = "DNS name of the load balancer"
  value       = aws_lb.main.dns_name
}

output "load_balancer_zone_id" {
  description = "The canonical hosted zone ID of the load balancer"
  value       = aws_lb.main.zone_id
}

# Target Group Outputs
output "target_group_arn" {
  description = "ARN of the target group"
  value       = aws_lb_target_group.main.arn
}

output "target_group_arn_suffix" {
  description = "ARN suffix for use with CloudWatch metrics"
  value       = aws_lb_target_group.main.arn_suffix
}

output "target_group_name" {
  description = "Name of the target group"
  value       = aws_lb_target_group.main.name
}

# Listener Outputs
output "https_listener_arn" {
  description = "ARN of the HTTPS listener"
  value       = length(aws_lb_listener.https) > 0 ? aws_lb_listener.https[0].arn : ""
}

output "http_listener_arn" {
  description = "ARN of the HTTP listener"
  value       = aws_lb_listener.http.arn
}

# Certificate Output
output "certificate_arn" {
  description = "ARN of the ACM certificate used"
  value       = length(data.aws_acm_certificate.main) > 0 ? data.aws_acm_certificate.main[0].arn : ""
}

# S3 Bucket Outputs
output "alb_logs_bucket_name" {
  description = "Name of the S3 bucket for ALB access logs"
  value       = aws_s3_bucket.alb_logs.bucket
}

output "alb_logs_bucket_arn" {
  description = "ARN of the S3 bucket for ALB access logs"
  value       = aws_s3_bucket.alb_logs.arn
}

# Route53 Outputs (conditional)
output "route53_record_name" {
  description = "Name of the Route53 record"
  value       = var.create_route53_record ? aws_route53_record.main[0].name : null
}

output "route53_record_fqdn" {
  description = "FQDN of the Route53 record"
  value       = var.create_route53_record ? aws_route53_record.main[0].fqdn : null
}

# WAF Outputs (conditional)
output "waf_web_acl_arn" {
  description = "ARN of the WAF Web ACL"
  value       = var.enable_waf ? aws_wafv2_web_acl.main[0].arn : null
}

output "waf_web_acl_id" {
  description = "ID of the WAF Web ACL"
  value       = var.enable_waf ? aws_wafv2_web_acl.main[0].id : null
}