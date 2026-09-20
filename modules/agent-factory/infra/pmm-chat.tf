# PMM-07 source-only, opt-in wiring. Applying this needs a scoped operator plan.
variable "chat_model_policy_enabled" {
  type    = bool
  default = false
}
variable "chat_model_control_endpoint" {
  type        = string
  default     = ""
  description = "Gateway URL ending /internal/v1/agent; required before opting in."
}
variable "chat_model_root_admission_arn" {
  type        = string
  default     = ""
  description = "Exact API/stage POST ARN for agent/internal/v1/agent/roots/admit."
  validation {
    condition     = var.chat_model_root_admission_arn == "" || can(regex("^arn:aws:execute-api:[a-z0-9-]+:[0-9]{12}:[a-z0-9]+/[A-Za-z0-9_-]+/POST/(agent/)?internal/v1/agent/roots/admit$", var.chat_model_root_admission_arn))
    error_message = "The chat producer receives only the exact root admission endpoint."
  }
}

variable "persona_model_mapping_enabled" {
  description = "Use saved persona models at dispatch without changing worker authority."
  type        = bool
  default     = false
}
