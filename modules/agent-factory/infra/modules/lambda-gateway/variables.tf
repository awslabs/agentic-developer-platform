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

variable "dynamodb_kms_key_arn" {
  description = "ARN of the agent-factory KMS key encrypting sessions and artifacts (distinct from the gateway identity-index key)."
  type        = string

  validation {
    condition     = can(regex("^arn:[a-z0-9-]+:kms:[a-z0-9-]+:[0-9]{12}:key/[a-zA-Z0-9-]+$", var.dynamodb_kms_key_arn))
    error_message = "The sessions/artifacts encryption key ARN must be supplied; an empty or wildcard KMS grant is not valid."
  }
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
  description = "GitHub org used to namespace the GH App secrets in Secrets Manager. Secrets must be stored at adp/<github_org>/gh-app-<persona>-{id,key} (ARC runner path; see modules/agent-factory/SETUP-GUIDE.md)."
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

variable "identity_index_table_name" {
  description = "Name of the gateway-managed identity-index DynamoDB table, read by the ingest Lambda's chat-dispatch ownership layer (issue #4233). Empty leaves the ownership layer inactive; the code-only org allowlist still applies."
  type        = string
  default     = ""
}

variable "identity_index_table_arn" {
  description = "ARN of the identity-index table. Empty skips the IAM policy entirely (nothing to grant on)."
  type        = string
  default     = ""
}

variable "identity_index_kms_key_arn" {
  description = "ARN of the gateway customer-managed KMS key encrypting the identity-index. Required alongside identity_index_table_arn — DynamoDB reads fail with AccessDenied without kms:Decrypt."
  type        = string
  default     = ""
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

variable "model_policy_enabled" {
  type    = bool
  default = false
}
variable "model_control_endpoint" {
  type    = string
  default = ""
}
variable "model_root_admission_arn" {
  type    = string
  default = ""
}
