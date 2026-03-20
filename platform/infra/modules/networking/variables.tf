variable "environment" {
  type        = string
  description = "Environment name (dev, test, prod)"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
}

variable "vpc_cidr" {
  type        = string
  description = "CIDR block for VPC"
  default     = "10.0.0.0/16"
}

variable "az_count" {
  type        = number
  description = "Number of availability zones to use"
  default     = 2
  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "AZ count must be between 2 and 3."
  }
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

variable "single_nat_gateway" {
  type        = bool
  description = "Use a single NAT gateway for all AZs (cost-saving for dev/test environments)"
  default     = true
}

# Issue #133: SECURITY FIX - ALB ingress restriction
# By default, restrict ALB to internal traffic only (VPC CIDR)
# Set to ["0.0.0.0/0"] for internet-facing ALB (not recommended)
variable "alb_ingress_cidr_blocks" {
  type        = list(string)
  description = "CIDR blocks allowed to access ALB. Defaults to VPC CIDR for internal ALB."
  default     = []  # Empty means use VPC CIDR
}

variable "alb_internal" {
  type        = bool
  description = "Whether the ALB is internal (true) or internet-facing (false)"
  default     = true  # Issue #133: Internal by default for security
}