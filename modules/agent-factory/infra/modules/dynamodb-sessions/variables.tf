variable "name_prefix" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "kms_key_arn" {
  description = "KMS key ARN for DynamoDB server-side encryption"
  type        = string
}
