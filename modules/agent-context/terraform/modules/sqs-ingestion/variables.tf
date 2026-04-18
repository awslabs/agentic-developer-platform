variable "cluster_name" {
  description = "Name of the EKS cluster (used as queue name prefix)"
  type        = string
}

variable "visibility_timeout" {
  description = "SQS visibility timeout in seconds (should exceed max worker runtime)"
  type        = number
  default     = 900 # 15 minutes — matches per-repo ingestion timeout
}

variable "message_retention_seconds" {
  description = "How long messages are retained in the queue"
  type        = number
  default     = 345600 # 4 days
}

variable "receive_wait_time_seconds" {
  description = "Long polling wait time (0 = short polling)"
  type        = number
  default     = 20 # Long polling for cost efficiency
}

variable "max_receive_count" {
  description = "Number of receive attempts before sending to DLQ"
  type        = number
  default     = 3
}

variable "irsa_role_name" {
  description = "IRSA role name to attach SQS policies to (empty = skip attachment)"
  type        = string
  default     = ""
}

variable "tags" {
  description = "Additional tags"
  type        = map(string)
  default     = {}
}
