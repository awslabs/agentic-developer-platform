# =============================================================================
# GitLab CE Infrastructure — Input Variables
# =============================================================================

variable "environment" {
  type        = string
  description = "Environment name: dev, test, or prod"

  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Environment must be dev, test, or prod."
  }
}

variable "aws_region" {
  type        = string
  description = "AWS region for all resources"
  default     = "us-east-1"
}

# -----------------------------------------------------------------------------
# EC2 Instance
# -----------------------------------------------------------------------------

variable "instance_type" {
  type        = string
  description = "EC2 instance type for GitLab CE (minimum m6i.xlarge for ≤50 users)"
  default     = "m6i.xlarge"
}

variable "root_volume_size" {
  type        = number
  description = "Root EBS volume size in GiB"
  default     = 100
}

variable "root_volume_type" {
  type        = string
  description = "Root EBS volume type"
  default     = "gp3"
}

variable "key_name" {
  type        = string
  description = "EC2 key pair name for SSH access (optional, SSM preferred)"
  default     = ""
}

# -----------------------------------------------------------------------------
# Networking
# -----------------------------------------------------------------------------

variable "gitlab_domain" {
  type        = string
  description = "FQDN for GitLab (e.g., gitlab.dev.adp.internal)"
}

variable "route53_zone_name" {
  type        = string
  description = "Route53 private hosted zone name (e.g., dev.adp.internal)"
}

variable "certificate_arn" {
  type        = string
  description = "ACM certificate ARN for HTTPS on the ALB (must cover gitlab_domain)"
  default     = ""
}

# -----------------------------------------------------------------------------
# Cognito OIDC
# -----------------------------------------------------------------------------

variable "cognito_user_pool_id" {
  type        = string
  description = "Cognito User Pool ID for OIDC authentication (from gateway module)"
}

variable "cognito_domain" {
  type        = string
  description = "Cognito hosted-UI domain prefix (e.g., adp-dev)"
}

# -----------------------------------------------------------------------------
# Backup
# -----------------------------------------------------------------------------

variable "backup_enabled" {
  type        = bool
  description = "Enable automated daily GitLab backups to S3"
  default     = false
}

variable "backup_s3_bucket_name" {
  type        = string
  description = "S3 bucket name for GitLab backups (created externally, referenced here). Unused when backup_enabled=true (bucket is created by this module)."
  default     = ""
}

variable "backup_retention_days" {
  type        = number
  description = "Number of days to retain backups in S3 before deletion"
  default     = 90
}

variable "backup_schedule" {
  type        = string
  description = "Cron expression for the SSM maintenance window (default: daily at 02:00 UTC)"
  default     = "cron(0 2 * * ? *)"
}
