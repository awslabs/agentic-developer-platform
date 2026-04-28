variable "environment" {
  type        = string
  description = "Environment name (dev, test, prod)"
  default     = "dev"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
  default     = "us-east-1"
}

variable "account_id" {
  type        = string
  description = "AWS account ID"
}

# ---------------------------------------------------------------------------
# VPC
# ---------------------------------------------------------------------------

variable "vpc_cidr" {
  type        = string
  description = "CIDR block for the Threat Research VPC"
  default     = "10.100.0.0/16"
}

variable "public_subnet_cidr" {
  type        = string
  description = "CIDR for the public subnet (NAT gateway)"
  default     = "10.100.0.0/24"
}

variable "private_subnet_cidrs" {
  type        = list(string)
  description = "CIDRs for private subnets (CAPE host)"
  default     = ["10.100.1.0/24", "10.100.2.0/24"]
}

variable "sandbox_subnet_cidr" {
  type        = string
  description = "CIDR for the sandbox documentation subnet (analysis VMs)"
  default     = "10.100.100.0/24"
}

# ---------------------------------------------------------------------------
# EC2 Host
# ---------------------------------------------------------------------------

variable "cape_instance_type" {
  type        = string
  description = "EC2 instance type for the CAPE host (must support nested virtualization)"
  default     = "c8i.4xlarge"
}

variable "cape_root_volume_size" {
  type        = number
  description = "Root EBS volume size in GB"
  default     = 200
}

variable "cape_data_volume_size" {
  type        = number
  description = "Data EBS volume size in GB (mounted at /opt/cape-data)"
  default     = 100
}

# ---------------------------------------------------------------------------
# VPC Peering (Phase 6)
# ---------------------------------------------------------------------------

variable "adp_vpc_id" {
  type        = string
  description = "VPC ID of the ADP platform VPC for peering. Empty string to skip peering."
  default     = ""
}

variable "adp_vpc_cidr" {
  type        = string
  description = "CIDR of the ADP platform VPC"
  default     = "10.0.0.0/16"
}

variable "adp_private_route_table_ids" {
  type        = list(string)
  description = "Route table IDs in the ADP VPC that need a route to the CAPE ALB"
  default     = []
}

variable "adp_eks_security_group_id" {
  type        = string
  description = "Security group ID of the ADP EKS pods (for ALB ingress rule)"
  default     = ""
}

# ---------------------------------------------------------------------------
# S3 / DynamoDB references (for IAM policy)
# ---------------------------------------------------------------------------

variable "sample_bucket_name" {
  type        = string
  description = "S3 bucket for chat artifacts (sample storage)"
  default     = "adp-dev-chat-artifacts"
}

variable "analysis_results_table" {
  type        = string
  description = "DynamoDB table for cyber analysis results"
  default     = "adp-dev-cyber-analysis-results"
}

# ---------------------------------------------------------------------------
# EKS Cluster (Issue #230)
# ---------------------------------------------------------------------------

variable "cyber_eks_cluster_version" {
  type        = string
  description = "Kubernetes version for the cyber EKS cluster"
  default     = "1.35"
}

variable "cyber_eks_public_access_cidrs" {
  type        = list(string)
  description = "CIDR blocks allowed to access the cyber EKS public endpoint"
  default     = ["0.0.0.0/0"]
}

variable "cyber_cluster_admin_principal_arns" {
  type        = list(string)
  description = "IAM principal ARNs to grant cluster-admin on the cyber EKS cluster"
  default     = []
}
