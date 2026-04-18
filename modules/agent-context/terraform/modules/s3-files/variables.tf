variable "cluster_name" {
  description = "Name of the EKS cluster"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "bucket_name" {
  description = "S3 bucket name for platform data"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace"
  type        = string
}

variable "vpc_id" {
  description = "VPC ID of the EKS cluster"
  type        = string
}

variable "subnet_ids" {
  description = "Subnet IDs for EFS mount targets"
  type        = list(string)
}

variable "node_security_group_id" {
  description = "Security group ID of EKS nodes"
  type        = string
}

variable "oidc_provider_url" {
  description = "OIDC provider URL for IRSA"
  type        = string
}

variable "tags" {
  description = "Additional tags"
  type        = map(string)
  default     = {}
}

variable "glacier_transition_days" {
  description = "Days before transitioning old object versions to Glacier"
  type        = number
  default     = 30
}

variable "efs_csi_driver_version" {
  description = "Version of the EFS CSI driver add-on"
  type        = string
  default     = "v3.1.1-eksbuild.1"
}
