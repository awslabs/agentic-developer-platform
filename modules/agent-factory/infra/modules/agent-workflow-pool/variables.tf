variable "enable_agent_workflow_runner" {
  type    = bool
  default = false
}
variable "agent_workflow_role_arn" {
  type    = string
  default = ""
}
variable "runner_role_arn" {
  type        = string
  default     = ""
  description = "Shared role, used only as an explicit non-equality check"
}
variable "runner_namespace" {
  type    = string
  default = "arc-runners"
}
variable "github_org" { type = string }
variable "github_repo" {
  type    = string
  default = ""
}
variable "github_config_secret" {
  type    = string
  default = "github-arc-secret"
}
variable "runner_image" {
  type    = string
  default = ""
}

variable "controller_namespace" {
  type    = string
  default = "arc-systems"
}
variable "controller_service_account" {
  type    = string
  default = "arc-gha-rs-controller"
}
