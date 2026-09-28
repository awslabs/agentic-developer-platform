variable "name_prefix" {
  description = "Platform resource name prefix"
  type        = string
}

variable "common_tags" {
  description = "Platform tags"
  type        = map(string)
  default     = {}
}

variable "enabled" {
  description = "Manage the regional logging configuration; disabling preserves the destinations and key"
  type        = bool
  default     = true
}

variable "retention_in_days" {
  description = "Finite retention shared by CloudWatch and S3"
  type        = number
  default     = 30

  validation {
    condition = contains([
      1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545,
      731, 1096, 1827, 2192, 2557, 2922, 3288, 3653,
    ], var.retention_in_days)
    error_message = "Use a supported, finite CloudWatch retention period (for example 7, 30, 90 or 365 days)."
  }
}
