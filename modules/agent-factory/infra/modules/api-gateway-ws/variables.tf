variable "name_prefix" {
  type = string
}

variable "stage_name" {
  type    = string
  default = "v1"
}

variable "ingest_lambda_arn" {
  type = string
}

variable "ingest_lambda_name" {
  type = string
}

variable "authorizer_lambda_invoke_arn" {
  description = "Invoke ARN of the existing gateway Lambda authorizer. REQUIRED — WebSocket connections must be authenticated."
  type        = string

  validation {
    condition     = var.authorizer_lambda_invoke_arn != ""
    error_message = "authorizer_lambda_invoke_arn must not be empty. The WebSocket $connect route requires a Lambda authorizer. Deploy the gateway module first (gateway-infra-apply) to provision the authorizer."
  }
}

variable "authorizer_lambda_function_name" {
  description = "Function name of the gateway Lambda authorizer. REQUIRED — WebSocket connections must be authenticated."
  type        = string

  validation {
    condition     = var.authorizer_lambda_function_name != ""
    error_message = "authorizer_lambda_function_name must not be empty. Both authorizer variables are required for WebSocket $connect authentication."
  }
}

variable "cloudwatch_kms_key_arn" {
  description = "ARN of the KMS key for CloudWatch Log Group encryption (CKV_AWS_158)"
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "disable_execute_api_endpoint" {
  type        = bool
  description = "Disable the AWS-assigned wss://<id>.execute-api.<region>.amazonaws.com endpoint so the API answers only on its custom domain. Set true wherever a custom domain is published: a WEBSOCKET API supports no resource policy and WAFv2 does not support WebSocket APIs, so the execute-api hostname has NO network-layer control of any kind and the $connect authorizer is its only gate. Default false, which is the AWS default and keeps a deployment without a custom domain reachable."
  default     = false
}
