variable "cluster_name" {
  description = "Name of the EKS cluster (used for resource naming)"
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

variable "collection_name" {
  description = "Name for the OpenSearch Serverless collection"
  type        = string
  default     = "graphrag-entities"
}

variable "oidc_provider_url" {
  description = "OIDC provider URL for IRSA"
  type        = string
}

variable "allow_public_access" {
  description = "Allow public access to the collection (set false for VPC-only, recommended for production)"
  type        = bool
  default     = false
}

variable "vpc_endpoint_ids" {
  description = "VPC endpoint IDs for network policy (when allow_public_access=false)"
  type        = list(string)
  default     = []
}

variable "additional_access_arns" {
  description = "Additional IAM ARNs to grant data access"
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Additional tags"
  type        = map(string)
  default     = {}
}
