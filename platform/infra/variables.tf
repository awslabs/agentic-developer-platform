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

variable "manage_ci_runner_cluster_admin" {
  description = <<-EOT
    Whether platform/infra grants the ARC runner role cluster-admin via an EKS
    access entry.

    Set false when modules/agent-factory/infra owns that principal's access
    entry (aws_eks_access_entry.runner), which is the case in any deployment
    where agent-factory has been applied. Leaving it true there makes
    platform/infra try to create an access entry that already exists — one entry
    per principal, so the apply fails — and, if it succeeded, would additively
    re-grant cluster-wide admin alongside agent-factory's deliberately
    namespace-scoped AmazonEKSEditPolicy (issue #1204).

    Trade-off when false: CI-run platform applies lose cluster-scope Kubernetes
    permissions, so the kubernetes_* resources in this module (namespaces,
    cluster roles) will fail. Platform applies then have to be run by a human
    operator holding cluster-admin. Defaults to true to preserve prior
    behaviour.
  EOT
  type        = bool
  default     = true
}

variable "eks_public_access_cidrs" {
  description = "CIDR blocks allowed to reach the EKS public API endpoint. Set via TF_VAR_eks_public_access_cidrs in the deploy scripts to the operator's current public IP (/32)."
  type        = list(string)
  default     = []
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

# Issue #4999: without this, NetworkPolicy objects in the cluster are accepted
# but never enforced. Default false — enabling it activates every existing
# policy at once, so it is a per-environment decision. See the module variable
# in modules/eks/variables.tf for the pre-enablement audit requirement.
variable "enable_network_policy_controller" {
  description = "Enable the EKS Auto Mode network-policy controller (what actually enforces NetworkPolicy objects). Audit existing policies for deny-without-allow gaps before enabling."
  type        = bool
  default     = false
}

variable "state_bucket" {
  description = "S3 bucket for Terraform state and CodeBuild source zips. Defaults to adp-terraform-state-<account_id>."
  type        = string
  default     = ""
}


variable "securityagent_log_retention_days" {
  description = "Retention for the nightly Security Agent log group (#4443)."
  type        = number
  default     = 30
}

variable "manage_bedrock_invocation_logging" {
  description = "Own the account/region Bedrock logging singleton in this platform state. Set false before first apply when another state owns it."
  type        = bool
  default     = true
}

variable "bedrock_invocation_logging_enabled" {
  description = "Enable provider invocation logging. False removes the logging configuration but retains its destinations and encryption key."
  type        = bool
  default     = true
}

variable "bedrock_invocation_log_retention_days" {
  description = "Retention for Bedrock invocation logs in CloudWatch and S3 (must be a finite CloudWatch retention value)"
  type        = number
  default     = 30
}
