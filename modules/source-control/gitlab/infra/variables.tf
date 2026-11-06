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

variable "portal_sso_enabled" {
  type        = bool
  default     = false
  description = "Configure ADP portal SSO on a fresh instance after provisioning the public key. Existing instances use configure-portal-sso.py."
}

variable "gitlab_package_version" {
  type        = string
  default     = "19.5.1-ce.0"
  description = "GitLab CE package version for new installations. Upgrade existing hosts through GitLab's required stops; a restore must use the backup's exact version."
  validation {
    condition     = can(regex("^[0-9][0-9A-Za-z.*+-]*$", var.gitlab_package_version))
    error_message = "Use a package version, without shell metacharacters."
  }
}

variable "portal_operator_role_arns" {
  type        = list(string)
  description = "Exact trusted IAM role ARNs allowed to seed and recover portal credentials. Required when portal IAM wiring is configured."
  default     = []
  validation {
    condition     = alltrue([for arn in var.portal_operator_role_arns : can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$", arn))])
    error_message = "Use exact IAM role ARNs, without wildcards or STS session ARNs."
  }
}

variable "gateway_role_name" {
  type        = string
  default     = ""
  description = "Existing gateway IAM role to grant signing-key read access. The optional GitLab module owns this policy."
}

variable "eks_oidc_provider_arn" {
  type        = string
  default     = ""
  description = "EKS OIDC provider ARN for the dedicated GitLab home reconciler."
}

variable "reconciler_database_resource_arn" {
  type        = string
  default     = ""
  description = "Exact rds-db:connect dbuser ARN for the dedicated home reconciler."
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

# -----------------------------------------------------------------------------
# CloudFront Integration
# -----------------------------------------------------------------------------

variable "cloudfront_domain" {
  type        = string
  description = "CloudFront distribution domain. When set, GitLab's external_url becomes https://<domain>/gitlab (relative_url_root mode) and SSM /adp/<env>/gitlab/url is updated accordingly."
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

variable "enable_cloudfront_vpc_origin_ingress" {
  type        = bool
  description = "Allow HTTP:80 ingress on the GitLab ALB from the CloudFront-VPCOrigins-Service-SG. Enable only after a CloudFront VPC origin exists in the VPC (the service SG is created with the first VPC origin)."
  default     = false
}
variable "secretsmanager_endpoint_security_group_ids" {
  description = "Security groups on private Secrets Manager endpoints that must admit HTTPS from GitLab for reconciler credential bootstrap."
  type        = set(string)
  default     = []
}

variable "runner_group_ids" {
  type        = set(number)
  description = "Existing verified numeric tenant-parent GitLab group IDs eligible for operator registration"
  default     = []
  validation {
    condition     = alltrue([for group_id in var.runner_group_ids : group_id > 0 && floor(group_id) == group_id])
    error_message = "Runner group IDs must be positive integers."
  }
}

variable "runner_registrar_role_name" {
  type        = string
  description = "Existing isolated registrar role, included in the admin operator allowlist."
  default     = ""
  validation {
    condition     = var.runner_registrar_role_name == "" || can(regex("^[A-Za-z0-9_+=,.@-]+$", var.runner_registrar_role_name))
    error_message = "Use an exact IAM role name."
  }
}

variable "runner_registrar_admin_role_arns" {
  description = "Explicit registrar-only admin-token authority, separate from portal signing-key operators. May contain only the configured registrar's existing ARN."
  type        = list(string)
  default     = []
  validation {
    condition = length(var.runner_registrar_admin_role_arns) <= 1 && alltrue([
      for arn in var.runner_registrar_admin_role_arns : can(regex("^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$", arn))
    ])
    error_message = "Use at most one exact registrar role ARN, without wildcards."
  }
}

variable "runner_ca_exporter_role_name" {
  type        = string
  description = "Existing isolated CA export role."
  default     = ""
  validation {
    condition     = var.runner_ca_exporter_role_name == "" || can(regex("^[A-Za-z0-9_+=,.@-]+$", var.runner_ca_exporter_role_name))
    error_message = "Use an exact IAM role name."
  }
}

variable "runner_manager_role_arns" {
  type        = map(string)
  description = "Numeric group ID to exact runner manager IAM role ARN."
  default     = {}
  validation {
    condition = alltrue([for group_id, arn in var.runner_manager_role_arns :
      can(regex("^[1-9][0-9]*$", group_id)) && can(regex("^arn:aws[a-z-]*:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$", arn))
    ])
    error_message = "Map numeric group IDs to exact IAM role ARNs without wildcards."
  }
}
