variable "table_name" {
  description = "Name of the DynamoDB table"
  type        = string
  default     = "adp-context-service-state"
}

variable "enable_pitr" {
  description = "Enable point-in-time recovery"
  type        = bool
  default     = true
}

variable "irsa_role_name" {
  description = "IRSA role name to attach DynamoDB policies to (empty = skip)"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Additional tags"
  type        = map(string)
  default     = {}
}
