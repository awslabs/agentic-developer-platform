variable "environment" {
  type = string
}

variable "name_prefix" {
  type = string
}

variable "kms_key_arn" {
  description = "KMS key ARN for DynamoDB server-side encryption"
  type        = string
}
