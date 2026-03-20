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

variable "cloudfront_distribution_arn" {
  type        = string
  description = "ARN of the CloudFront distribution for bucket policy"
}

variable "cors_allowed_origins" {
  type        = list(string)
  description = "List of allowed origins for CORS configuration"
  default     = ["*"]
}

variable "noncurrent_version_expiration_days" {
  type        = number
  description = "Number of days to retain non-current object versions"
  default     = 30
}
