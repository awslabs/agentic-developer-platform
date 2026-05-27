variable "environment" {
  description = "Deployment environment (dev, prod)"
  type        = string
}

variable "security_scan_glacier_days" {
  description = "Days before transitioning SARIF files to Glacier"
  type        = number
  default     = 90
}

variable "security_scan_expire_days" {
  description = "Days before expiring SARIF files"
  type        = number
  default     = 365
}
