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
  description = "Registration secret created by modules/arc-runner (github-arc-secret); referenced by name."
  type        = string
  default     = "github-arc-secret"
}
variable "runner_service_account" {
  description = "Service account created by modules/arc-runner for the org pool; the deploy pool keeps the same identity."
  type        = string
  default     = "github-runner-sa"
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
variable "min_runners" {
  type    = number
  default = 1
}
variable "max_runners" {
  type    = number
  default = 8
}
variable "node_selector" {
  description = "Optional nodeSelector for runner pods (e.g. {\"adp.aws/trust\" = \"deployment\"} to move onto the trusted NodePool). Empty = schedule anywhere."
  type        = map(string)
  default     = {}
}
variable "tolerations" {
  description = "Optional tolerations for runner pods (e.g. the adp.aws/trust=deployment:NoSchedule taint). Empty = none."
  type = list(object({
    key      = string
    operator = string
    value    = optional(string)
    effect   = string
  }))
  default = []
}
