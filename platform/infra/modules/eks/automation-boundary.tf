# The operator owns this ceiling outside application Terraform state. Set this
# input during trusted-automation cutover; null retains operator bootstrap mode.
variable "automation_permissions_boundary_arn" {
  type        = string
  default     = null
  description = "Immutable operator-reviewed workload permissions boundary. Required for roles managed by trusted automation."
  validation {
    condition     = var.automation_permissions_boundary_arn == null ? true : can(regex("^arn:aws:iam::[0-9]{12}:policy/[A-Za-z0-9_/-]+$", var.automation_permissions_boundary_arn))
    error_message = "Use an exact operator-owned IAM permissions boundary ARN."
  }
}
