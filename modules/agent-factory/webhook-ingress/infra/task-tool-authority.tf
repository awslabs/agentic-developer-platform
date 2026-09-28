# Independently deployed tool services remain behind exact IAM routes. These
# exceptions affect the protected worker boundary only; legacy worker retirement
# and existing internal gateway restrictions remain unchanged.
variable "task_tool_invoke_resources" {
  description = "Explicit stage-qualified POST tool routes admitted by the protected worker authority boundary."
  type        = list(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.task_tool_invoke_resources : can(regex("^arn:aws:execute-api:[a-z0-9-]+:[0-9]{12}:[a-z0-9]+/[A-Za-z0-9_-]+/POST/tools/[a-z0-9][a-z0-9_/-]*$", arn))])
    error_message = "Tool exceptions require an exact API, account, stage, POST method and tools path; wildcards and internal routes are refused."
  }
}
