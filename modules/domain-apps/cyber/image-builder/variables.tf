variable "environment" {
  type        = string
  description = "Environment name (dev, test, prod)"
  default     = "dev"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
  default     = "us-east-1"
}

variable "account_id" {
  type        = string
  description = "AWS account ID"
}

# ---------------------------------------------------------------------------
# S3 Assets Bucket
# ---------------------------------------------------------------------------

variable "assets_bucket_name" {
  type        = string
  description = "Name of the S3 bucket for CAPE assets (qcow2 images, etc.)"
  default     = "adp-dev-cape-assets"
}

# ---------------------------------------------------------------------------
# Build Host
# ---------------------------------------------------------------------------

variable "build_host_enabled" {
  type        = bool
  description = "Whether to create the ephemeral build host. Set to false after build completes."
  default     = false
}

variable "build_instance_type" {
  type        = string
  description = "EC2 instance type for the build host (must support nested virtualization)"
  default     = "c8i.4xlarge"
}

variable "build_root_volume_size" {
  type        = number
  description = "Root EBS volume size in GB for the build host"
  default     = 300
}

variable "vpc_name" {
  type        = string
  description = "Name tag of the VPC to deploy the build host into. Must have SSM VPC endpoints. Defaults to adp-<env>-cyber-vpc which has them."
  default     = ""
}

variable "subnet_id_override" {
  type        = string
  description = "Optional: force a specific private subnet ID. Leave empty to auto-select the first private subnet in the target VPC."
  default     = ""
}

# ---------------------------------------------------------------------------
# Auto-teardown
# ---------------------------------------------------------------------------

variable "idle_cpu_threshold" {
  type        = number
  description = "CPU utilization % below which the host is considered idle"
  default     = 5
}

variable "idle_period_seconds" {
  type        = number
  description = "Duration in seconds the host must be idle before auto-termination (4 hours)"
  default     = 14400
}

variable "builder_permissions_boundary_arn" {
  type        = string
  description = "Operator-owned workload ceiling required by the Windows CI identity."
  default     = null
}

variable "builder_name_suffix" {
  type        = string
  description = "Separate automated builders from pre-existing standalone build resources."
  default     = ""
  validation {
    condition     = can(regex("^[a-z0-9-]*$", var.builder_name_suffix))
    error_message = "Builder suffix must contain only lowercase letters, digits and hyphens."
  }
}
