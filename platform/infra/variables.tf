variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
}

variable "name_prefix" {
  description = "Prefix for resource names. Defaults to 'adp-<environment>'."
  type        = string
  default     = ""
}

variable "vpc_cidr" {
  description = "CIDR block for VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to use (2 or 3)"
  type        = number
  default     = 2
}

variable "single_nat_gateway" {
  description = "Use a single NAT gateway across all AZs (cost-saving for dev)"
  type        = bool
  default     = true
}

variable "eks_cluster_version" {
  description = "Kubernetes version for the EKS cluster"
  type        = string
  default     = "1.35"
}

variable "eks_node_instance_types" {
  description = "Instance types for EKS Auto Mode node group"
  type        = list(string)
  default     = ["m5.large", "m5.xlarge"]
}

variable "eks_node_desired_size" {
  description = "Desired number of EKS nodes"
  type        = number
  default     = 2
}

variable "eks_node_min_size" {
  description = "Minimum number of EKS nodes"
  type        = number
  default     = 1
}

variable "eks_node_max_size" {
  description = "Maximum number of EKS nodes"
  type        = number
  default     = 10
}

variable "eks_public_access_cidrs" {
  description = "CIDR blocks allowed to reach the EKS public API endpoint. Set via TF_VAR_eks_public_access_cidrs in the deploy scripts to the operator's current public IP (/32)."
  type        = list(string)
}

variable "create_instance_profile" {
  description = "Whether to create an IAM instance profile for EKS nodes (set false if you lack iam:TagInstanceProfile)"
  type        = bool
  default     = true
}

variable "ecr_repositories" {
  description = "List of ECR repository names to create"
  type        = list(string)
  default = [
    "adp-gateway",
    "adp-agent-runtime",
    "adp-skill-registry",
    "adp-agent-gateway",
  ]
}

variable "extra_cluster_admin_principal_arns" {
  description = "Additional IAM principal ARNs (users/roles) to grant EKS cluster-admin. The deploying caller is added automatically."
  type        = list(string)
  default     = []
}

variable "enable_container_insights" {
  description = "Enable CloudWatch Container Insights via the amazon-cloudwatch-observability addon"
  type        = bool
  default     = false
}

variable "state_bucket" {
  description = "S3 bucket for Terraform state and CodeBuild source zips. Defaults to adp-terraform-state-<account_id>."
  type        = string
  default     = ""
}

