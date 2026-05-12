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
  description = "Invoke ARN of the existing gateway Lambda authorizer. Empty = no auth."
  type        = string
  default     = ""
}

variable "authorizer_lambda_function_name" {
  description = "Function name of the gateway Lambda authorizer (for aws_lambda_permission). Required when authorizer_lambda_invoke_arn is set."
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}
