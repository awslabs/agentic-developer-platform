variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "waf_rate_limit" {
  description = "Maximum requests per 5-minute window per IP"
  type        = number
  default     = 1000
}

variable "sqs_visibility_timeout" {
  description = "SQS visibility timeout in seconds (should exceed max agent run time)"
  type        = number
  default     = 7200
}

variable "sqs_message_retention" {
  description = "SQS message retention in seconds"
  type        = number
  default     = 345600 # 4 days
}

variable "sqs_max_receive_count" {
  description = "Max receive count before message moves to DLQ"
  type        = number
  default     = 3
}

variable "lambda_runtime" {
  description = "Lambda runtime"
  type        = string
  default     = "python3.12"
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds"
  type        = number
  default     = 30
}

variable "lambda_memory_size" {
  description = "Lambda memory in MB"
  type        = number
  default     = 256
}
