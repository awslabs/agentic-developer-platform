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

variable "vpc_id" {
  type        = string
  description = "ID of the VPC"
}

variable "public_subnet_ids" {
  type        = list(string)
  description = "List of public subnet IDs for ALB"
}

variable "alb_security_group_id" {
  type        = string
  description = "Security group ID for ALB"
}

variable "domain_name" {
  type        = string
  description = "Domain name for the ALB (e.g., gateway.company.com)"
}

variable "certificate_domain" {
  type        = string
  description = "Domain for ACM certificate (e.g., *.company.com or gateway.company.com)"
}

variable "create_route53_record" {
  type        = bool
  description = "Whether to create Route53 A record for the domain"
  default     = false
}

variable "route53_zone_name" {
  type        = string
  description = "Route53 hosted zone name (required if create_route53_record is true)"
  default     = ""
}

variable "enable_waf" {
  type        = bool
  description = "Enable AWS WAF Web ACL for the ALB"
  default     = false
}