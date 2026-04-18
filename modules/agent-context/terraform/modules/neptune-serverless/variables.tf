variable "cluster_name" {
  description = "Name of the EKS cluster (used for Neptune cluster identifier)"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace for IRSA binding"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID for the Neptune cluster"
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for the Neptune subnet group"
  type        = list(string)
}

variable "node_security_group_id" {
  description = "Security group ID of EKS nodes (allowed to connect to Neptune)"
  type        = string
}

variable "oidc_provider_url" {
  description = "OIDC provider URL for IRSA"
  type        = string
}

variable "neptune_engine_version" {
  description = "Neptune engine version"
  type        = string
  default     = "1.3.4.0"
}

variable "min_capacity" {
  description = "Minimum NCUs for Neptune Serverless (scales to zero below this)"
  type        = number
  default     = 1.0
}

variable "max_capacity" {
  description = "Maximum NCUs for Neptune Serverless"
  type        = number
  default     = 16.0
}

variable "skip_final_snapshot" {
  description = "Skip final snapshot on cluster deletion"
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Enable deletion protection"
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional tags"
  type        = map(string)
  default     = {}
}
