variable "environment" {
  type        = string
  description = "Environment name (dev, test, prod)"
}

variable "name_prefix" {
  type        = string
  description = "Name prefix for resources"
}

variable "common_tags" {
  type        = map(string)
  description = "Common tags to apply to all resources"
  default     = {}
}

variable "s3_bucket_regional_domain_name" {
  type        = string
  description = "Regional domain name of the S3 bucket for origin"
}

variable "s3_bucket_id" {
  type        = string
  description = "ID of the S3 bucket for origin"
}

variable "price_class" {
  type        = string
  description = "CloudFront distribution price class"
  default     = ""
  validation {
    condition     = var.price_class == "" || contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "Price class must be PriceClass_100, PriceClass_200, or PriceClass_All."
  }
}

variable "custom_domain_name" {
  type        = string
  description = "Custom domain name for CloudFront (optional)"
  default     = ""
}

variable "acm_certificate_arn" {
  type        = string
  description = "ACM certificate ARN for custom domain (required if custom_domain_name is set)"
  default     = ""
}

variable "waf_web_acl_arn" {
  type        = string
  description = "WAF web ACL ARN to associate with CloudFront (optional)"
  default     = ""
}

variable "log_bucket_domain_name" {
  type        = string
  description = "Domain name of S3 bucket for CloudFront access logs (optional)"
  default     = ""
}

variable "log_prefix" {
  type        = string
  description = "Prefix for CloudFront access logs"
  default     = "cloudfront-logs/"
}

variable "default_ttl" {
  type        = number
  description = "Default TTL for cached objects in seconds"
  default     = 86400 # 1 day
}

variable "max_ttl" {
  type        = number
  description = "Maximum TTL for cached objects in seconds"
  default     = 31536000 # 1 year
}

variable "min_ttl" {
  type        = number
  description = "Minimum TTL for cached objects in seconds"
  default     = 0
}

variable "alb_domain_name" {
  type        = string
  description = "Domain name of the ALB for API backend proxy (optional). When set, CloudFront proxies /api/* to the ALB."
  default     = ""
}

# =============================================================================
# VPC Origin Configuration (for internal ALB)
# =============================================================================
# These variables are used when the ALB is internal and CloudFront needs to
# access it via VPC Origin. VPC Origins allow CloudFront to connect to private
# resources within a VPC.

variable "enable_vpc_origin" {
  type        = bool
  description = "Enable VPC Origin for internal ALB. When true, CloudFront uses VPC Origin instead of custom origin."
  default     = false
}

variable "internal_alb_arn" {
  type        = string
  description = "ARN of the internal ALB for VPC Origin. Required when enable_vpc_origin is true."
  default     = ""
}

variable "internal_alb_dns" {
  type        = string
  description = "DNS hostname of the internal ALB. Used as `domain_name` for the VPC origin (must not contain colons — see PR #1090)."
  default     = ""
}

variable "vpc_origin_read_timeout" {
  type        = number
  description = "Origin read timeout in seconds for VPC Origin. CloudFront caps this at 60s for VPC origins (custom origins allow up to 180s, but VPC origins are stricter — confirmed live: AWS rejects values >60 with InvalidOriginReadTimeout)."
  default     = 60
  validation {
    condition     = var.vpc_origin_read_timeout >= 1 && var.vpc_origin_read_timeout <= 60
    error_message = "VPC Origin read timeout must be between 1 and 60 seconds (AWS API limit)."
  }
}

variable "vpc_origin_keepalive_timeout" {
  type        = number
  description = "Origin keepalive timeout in seconds for VPC Origin."
  default     = 60
  validation {
    condition     = var.vpc_origin_keepalive_timeout >= 1 && var.vpc_origin_keepalive_timeout <= 60
    error_message = "VPC Origin keepalive timeout must be between 1 and 60 seconds."
  }
}

# =============================================================================
# GitLab VPC Origin Configuration
# =============================================================================
# When both gitlab_origin_dns and gitlab_origin_arn are non-empty, a VPC Origin
# and /gitlab/* ordered cache behavior are created. Empty defaults mean zero
# change to existing deployments.

variable "gitlab_origin_dns" {
  type        = string
  description = "DNS hostname of the GitLab internal ALB. When non-empty (along with gitlab_origin_arn), enables the /gitlab/* cache behavior."
  default     = ""
}

variable "gitlab_origin_arn" {
  type        = string
  description = "ARN of the GitLab internal ALB for VPC Origin. Required together with gitlab_origin_dns to enable the GitLab origin."
  default     = ""
}
