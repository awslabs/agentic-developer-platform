# CloudFront Distribution Outputs

output "distribution_id" {
  description = "ID of the CloudFront distribution"
  value       = aws_cloudfront_distribution.frontend.id
}

output "distribution_arn" {
  description = "ARN of the CloudFront distribution"
  value       = aws_cloudfront_distribution.frontend.arn
}

output "distribution_domain_name" {
  description = "Domain name of the CloudFront distribution"
  value       = aws_cloudfront_distribution.frontend.domain_name
}

output "distribution_hosted_zone_id" {
  description = "CloudFront Route 53 zone ID"
  value       = aws_cloudfront_distribution.frontend.hosted_zone_id
}

output "distribution_status" {
  description = "Status of the CloudFront distribution"
  value       = aws_cloudfront_distribution.frontend.status
}

output "oac_id" {
  description = "ID of the Origin Access Control"
  value       = aws_cloudfront_origin_access_control.frontend.id
}

output "response_headers_policy_id" {
  description = "ID of the response headers policy"
  value       = aws_cloudfront_response_headers_policy.security_headers.id
}

# VPC Origin Outputs
output "vpc_origin_id" {
  description = "ID of the CloudFront VPC Origin (empty if not using VPC Origin)"
  value       = length(aws_cloudfront_vpc_origin.api) > 0 ? aws_cloudfront_vpc_origin.api[0].id : ""
}

output "vpc_origin_arn" {
  description = "ARN of the CloudFront VPC Origin (empty if not using VPC Origin)"
  value       = length(aws_cloudfront_vpc_origin.api) > 0 ? aws_cloudfront_vpc_origin.api[0].arn : ""
}

output "vpc_origin_enabled" {
  description = "Whether VPC Origin is enabled for API traffic"
  value       = var.enable_vpc_origin
}

output "strip_api_prefix_function_arn" {
  description = "ARN of the CloudFront Function that strips /api prefix"
  value       = aws_cloudfront_function.strip_api_prefix.arn
}
