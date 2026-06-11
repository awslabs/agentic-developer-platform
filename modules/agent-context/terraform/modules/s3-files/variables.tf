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

variable "mountpoint_s3_csi_driver_version" {
  description = "Version of the Mountpoint for Amazon S3 CSI driver EKS add-on"
  type        = string
  default     = "v1.12.0-eksbuild.1"
}
