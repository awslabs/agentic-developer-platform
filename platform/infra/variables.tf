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

variable "additional_private_subnet_ids_by_az" {
  description = <<-EOT
    Additional ALREADY-EXISTING private subnets to add to the EKS cluster's
    subnet set, keyed by the availability zone each is expected to be in:

      {"us-east-1a" = "subnet-0123456789abcdef0"}

    Why (#5830): when the cluster's original private subnets exhaust their IP
    addresses, the CNI fails every new pod with "failed to assign an IP address
    to container". Auto Mode's AWS-managed `default` NodeClass takes its subnets
    from the cluster's resourcesVpcConfig.subnetIds, so widening that set is the
    supported way to give new nodes addresses without editing the NodeClass.

    ADDITIVE: appended to the networking module's private subnets, which always
    remain in the set. Only the EKS cluster's own subnet set is affected — RDS,
    load balancers, Lambdas and VPC endpoints are not.

    Empty (the default) leaves an un-widened cluster as it is, but it is NOT a
    no-op once subnets have been added: empty then plans their REMOVAL and
    re-breaks pod IP assignment for nodes launched afterwards. That is why the
    deployment paths resolve this against the live cluster (see CI, below).

    Creates nothing. Every entry is checked at plan time and the plan FAILS
    unless the subnet is in this VPC, is in the zone it is keyed by, assigns no
    public IPs, and routes 0.0.0.0/0 via NAT rather than an internet gateway.

    NOT assigned in environments/<env>/platform.tfvars, deliberately — same
    reason as extra_cluster_admin_principal_arns below: these IDs are
    account-specific, and a `-var-file` assignment (even `= {}`) OVERRIDES
    TF_VAR_ environment variables, so an explicit assignment there would
    silently defeat both the operator export and CI's passthrough. The declared
    default ({}) already keeps the shipped repo portable.

      export TF_VAR_additional_private_subnet_ids_by_az='{"us-east-1a":"subnet-..."}'

    CI: set the ADDITIONAL_PRIVATE_SUBNETS_BY_AZ repository variable. It is NOT
    passed straight through — on a cluster already widened, an unset or stale
    variable would resolve to this default and plan the additions away. Both
    platform-infra-apply.yml and `--update` runs resolve the effective map
    against the LIVE cluster (platform/scripts/capacity_subnets.py): unset or
    blank retains, a map omitting a live addition is REFUSED, and narrowing
    needs an explicit authorisation. A bare `terraform apply` with the variable
    unset bypasses that and WILL plan the additions away.
  EOT
  type        = map(string)
  default     = {}
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
  description = "Retired: ordinary repository runners cannot receive Kubernetes administration. Use the independently bootstrapped trusted deployment identity."
  type        = bool
  default     = false
  validation {
    condition     = !var.manage_ci_runner_cluster_admin
    error_message = "Runner cluster-admin is forbidden; bootstrap platform/automation-infra and keep this value false."
  }
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
    "adp-chat-agent",
  ]
}

variable "retained_upgrade_kms_key_ids" {
  description = "Keys retained in Terraform state after recovery from an interrupted ownership migration"
  type        = set(string)
  default     = []
}

variable "ecr_repository_encryption" {
  description = "Existing per-repository encryption to retain during upgrades; new repositories use the managed ECR key"
  type = map(object({
    encryption_type = string
    kms_key         = optional(string)
  }))
  default = {}
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

variable "eks_endpoint_public_access" {
  description = "Whether the EKS API has a public endpoint. Upgrades retain the live setting."
  type        = bool
  default     = true
}

variable "eks_endpoint_private_access" {
  description = "Whether the EKS API has a private endpoint. Upgrades retain the live setting."
  type        = bool
  default     = true
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

variable "manage_ecr_registry_scanning" {
  description = "Own account/region ECR registry scanning. Existing ownership must be relinquished without deletion before disabling."
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

variable "gateway_customer_role_arns" {
  description = "Exact customer IAM role ARNs approved for gateway connections and routing. Empty denies cross-account customer assumptions."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for arn in var.gateway_customer_role_arns : can(regex("^arn:aws(-[a-z]+)*:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+$", arn))])
    error_message = "Customer role approvals must be exact IAM role ARNs without wildcards."
  }
}
