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

variable "enable_ws_policies" {
  description = "Whether to create IAM policies for WebSocket API ManageConnections. Set to true when a WS API is being created in the same apply — avoids count depending on a computed ARN."
  type        = bool
  default     = false
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "github_org" {
  description = "GitHub org used to namespace the GH App secrets in Secrets Manager. Secrets must be stored at adp/<github_org>/gh-app-<persona>-{id,key} by the onboarding script (platform/scripts/create-github-apps.sh)."
  type        = string
}

variable "artifacts_bucket_arn" {
  description = "ARN of the S3 bucket for chat artifacts (presigned URL signing)."
  type        = string
}

variable "artifacts_bucket_name" {
  description = "Name of the S3 bucket for chat artifacts."
  type        = string
}

variable "artifacts_table_arn" {
  description = "ARN of the DynamoDB table for chat artifacts catalog."
  type        = string
}

variable "artifacts_table_name" {
  description = "Name of the DynamoDB table for chat artifacts catalog."
  type        = string
}

variable "classifier_model" {
  description = "Bedrock model ID used by the ingest classifier. Haiku 4.5 is ~5x faster than Sonnet 4.6 for the short routing call — the classifier sees <2KB of context and returns ~200 bytes of JSON, so Haiku's accuracy gap is negligible but the latency win is the difference between 400ms and 2s per user turn."
  type        = string
  default     = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "cloudwatch_kms_key_arn" {
  description = "ARN of the KMS key for CloudWatch Log Group encryption (CKV_AWS_158)"
  type        = string
  default     = ""
}
