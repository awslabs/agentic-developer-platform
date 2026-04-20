variable "name_prefix" {
  type = string
}

variable "environment" {
  type = string
}

variable "aws_region" {
  type = string
}

variable "ingest_source_dir" {
  type = string
}

variable "response_source_dir" {
  type = string
}

variable "input_queue_url" {
  type = string
}

variable "input_queue_arn" {
  type = string
}

variable "response_queue_url" {
  type = string
}

variable "response_queue_arn" {
  type = string
}

variable "sessions_table_name" {
  type = string
}

variable "sessions_table_arn" {
  type = string
}

variable "ws_api_endpoint" {
  type    = string
  default = ""
}

variable "ws_api_id" {
  type    = string
  default = ""
}

variable "ws_execution_arn" {
  type    = string
  default = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "github_org" {
  description = "GitHub org used to namespace the GH App secrets in Secrets Manager. Secrets must be stored at adp/<github_org>/gh-app-<persona>-{id,key} by the onboarding script (platform/scripts/create-github-apps.sh)."
  type        = string
}

variable "account_id" {
  description = "AWS account ID (for constructing secret ARNs)."
  type        = string
}
