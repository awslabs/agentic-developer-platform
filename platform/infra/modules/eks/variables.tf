variable "environment" {
  type        = string
  description = "Environment name (dev, test, prod)"
}

variable "name_prefix" {
  type        = string
  description = "Name prefix for resources"
}

variable "common_tags" {
  type        = map(string)
  description = "Common tags to apply to all resources"
  default     = {}
}

variable "vpc_id" {
  type        = string
  description = "ID of the VPC"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "List of private subnet IDs"
}

variable "private_subnet_availability_zones" {
  type        = list(string)
  description = <<-DESC
    Availability zones of var.private_subnet_ids, in the same order. Used only to
    check that an entry in additional_private_subnet_ids_by_az names a zone the
    cluster already has capacity in. Empty disables that one check (the VPC,
    zone-membership, public-IP and private-routing checks still apply).
  DESC
  default     = []
}

variable "additional_private_subnet_ids_by_az" {
  type        = map(string)
  description = <<-DESC
    Additional ALREADY-EXISTING private subnets to add to this cluster's subnet
    set, keyed by the availability zone each subnet is expected to be in
    (e.g. {"us-east-1a" = "subnet-0123456789abcdef0"}).

    Why this exists (#5830): the cluster's original subnets can exhaust their
    private IP addresses, at which point the CNI fails every new pod with
    "failed to assign an IP address to container". EKS Auto Mode's AWS-managed
    `default` NodeClass takes its subnets from the cluster's own
    resourcesVpcConfig.subnetIds, so widening that set is the supported way to
    give new nodes more addresses — the NodeClass itself must not be edited.

    ADDITIVE, never a replacement: entries are appended to var.private_subnet_ids,
    which always stays in the cluster's subnet set. Empty (the default, and the
    shipped configuration) leaves the subnet set exactly as it is today.

    This widens the CLUSTER's subnet set only. Every other subnet consumer
    (RDS subnet group, load balancers, Lambda VPC config, VPC endpoints) selects
    from var.private_subnet_ids / the networking module and is unaffected.

    Creates nothing: no subnet, NAT gateway or VPC endpoint. Supply only subnets
    that already exist, already have free addresses, and already route outbound
    through the existing private path. Each entry is checked at plan time (see
    the data-source postconditions in main.tf) and the plan FAILS unless the
    subnet is in var.vpc_id, is really in the zone it is keyed by, assigns no
    public IPs, and reaches 0.0.0.0/0 via NAT rather than an internet gateway.

    Keyed by zone deliberately: it makes "one subnet per availability zone"
    structural rather than something a later reviewer has to verify by eye, and
    it turns a subnet pasted under the wrong zone into a refused plan instead of
    silent loss of zone coverage.

    Account-specific by nature, so it is NOT set in the shipped environment
    files. Supply it per-invocation:
      export TF_VAR_additional_private_subnet_ids_by_az='{"us-east-1a":"subnet-..."}'
    For CI, set the ADDITIONAL_PRIVATE_SUBNETS_BY_AZ repository variable, which
    platform-infra-apply.yml passes through. `--update` runs rediscover the live
    cluster's extra subnets and retain them (platform/scripts/upgrade-state.py),
    so a later routine update cannot silently shrink the subnet set back.

    Existing nodes are not moved by this; only newly launched nodes can use the
    added subnets.
  DESC
  default     = {}

  validation {
    condition     = alltrue([for id in values(var.additional_private_subnet_ids_by_az) : can(regex("^subnet-[0-9a-f]{8,17}$", id))])
    error_message = "Every additional_private_subnet_ids_by_az value must be an AWS subnet id such as subnet-0123456789abcdef0."
  }

  validation {
    condition     = length(values(var.additional_private_subnet_ids_by_az)) == length(distinct(values(var.additional_private_subnet_ids_by_az)))
    error_message = "additional_private_subnet_ids_by_az must not name the same subnet under two availability zones."
  }
}

variable "eks_security_group_id" {
  type        = string
  description = "Security group ID for EKS cluster"
}

variable "cluster_version" {
  type        = string
  description = "Kubernetes version for EKS cluster"
  default     = "1.35"
}

variable "node_group_instance_types" {
  type        = list(string)
  description = "Instance types for EKS node group"
  default     = ["t3.medium"]
}

variable "node_group_desired_size" {
  type        = number
  description = "Desired number of nodes in EKS node group"
  default     = 2
}

variable "node_group_max_size" {
  type        = number
  description = "Maximum number of nodes in EKS node group"
  default     = 5
}

variable "node_group_min_size" {
  type        = number
  description = "Minimum number of nodes in EKS node group"
  default     = 1
}

variable "eks_cluster_role_arn" {
  type        = string
  description = "ARN of the EKS cluster service role (trust: eks.amazonaws.com)"
}

variable "node_group_role_arn" {
  type        = string
  description = "ARN of the EKS node group IAM role"
  default     = ""
}

variable "eks_public_access_cidrs" {
  type        = list(string)
  description = "CIDR blocks allowed to access EKS public endpoint"
  # No default — must be explicitly set per environment to prevent accidental 0.0.0.0/0 exposure
}

variable "ci_runner_role_arn" {
  type        = string
  description = "ARN of the CI runner IAM role for EKS API access"
  default     = ""
}

variable "cluster_admin_principal_arns" {
  type        = list(string)
  description = "IAM principal ARNs to grant AmazonEKSClusterAdminPolicy on this cluster (e.g. the deploying user/role, CI runner). Required so the Kubernetes provider in this module can create namespaces/service accounts on first apply."
  default     = []
}

# IRSA Gateway Service Configuration
variable "enable_rds_iam_auth" {
  type        = bool
  description = "Enable RDS IAM authentication policy on the gateway IRSA role"
  default     = false
}

variable "rds_db_username" {
  type        = string
  description = "The database username for RDS IAM authentication"
  default     = "bgadmin"
}

variable "enable_elasticache_iam_auth" {
  type        = bool
  description = "Enable ElastiCache IAM authentication policy on the gateway IRSA role"
  default     = false
}

variable "redis_replication_group_id" {
  type        = string
  description = "The ID of the Redis replication group for IAM auth"
  default     = ""
}

variable "redis_iam_user_id" {
  type        = string
  description = "The ID of the Redis IAM user for IAM auth"
  default     = ""
}

variable "pool_account_arns" {
  type        = list(string)
  description = "List of AWS account ARNs for cross-account Bedrock pool access"
  default     = []
}

# Issue #226: Cognito User Pool ID for Cognito-backed entity list endpoints
variable "cognito_user_pool_id" {
  type        = string
  description = "The Cognito User Pool ID for Cognito read permissions (e.g., us-east-1_AbCdEfGhI). If empty, Cognito IAM policy is not created."
  default     = ""
}

# Issue #143: Chat Logging — S3 and Comprehend permissions for IRSA role
variable "chat_logs_bucket_arn" {
  type        = string
  description = "ARN of the S3 bucket for chat logs. If empty, S3 chat logging policy is not created."
  default     = ""
}

variable "enable_comprehend_pii" {
  type        = bool
  description = "Enable Comprehend PII detection permissions on the gateway IRSA role"
  default     = false
}


# Container Insights — CloudWatch Observability addon
variable "enable_container_insights" {
  type        = bool
  description = "Enable CloudWatch Container Insights via the amazon-cloudwatch-observability EKS addon. Ships pod logs and metrics to CloudWatch."
  default     = false
}

variable "endpoint_public_access" {
  description = "Enable the EKS public API endpoint."
  type        = bool
  default     = true
}

variable "endpoint_private_access" {
  description = "Enable the EKS private API endpoint."
  type        = bool
  default     = true
}

# NetworkPolicy enforcement — Auto Mode network-policy controller (#4999)
variable "enable_network_policy_controller" {
  type        = bool
  description = <<-DESC
    Enable the EKS Auto Mode network-policy controller, which is what actually
    ENFORCES Kubernetes NetworkPolicy objects. While false, NetworkPolicies are
    accepted by the apiserver and have no effect: no PolicyEndpoint objects are
    created and a deny-all policy does not deny anything (#4999, evaluation
    #3967 check W1-04).

    Defaults to false because flipping it on is not additive — it makes every
    NetworkPolicy already present in the cluster take effect simultaneously. Any
    pod that is selected by a deny policy but matched by no allow policy loses
    that traffic at the TCP layer, with no pod restart and no error surfaced to
    the workload. Before enabling in an environment, audit the existing policies
    for exactly that gap (in adp-agents this is why the ADOT collector needed
    an egress policy first, or all agent telemetry would have stopped silently).

    Rollback: set back to false and apply. Enforcement stops and the previous
    unenforced behaviour returns.
  DESC
  default     = false
}

# EKS Pod Identity for the gateway service account (#5051)
variable "enable_gateway_pod_identity" {
  type        = bool
  description = <<-DESC
    Create EKS Pod Identity associations for the gateway-service service account,
    so the gateway obtains AWS credentials through the platform instead of a
    stored access key.

    Defaults to false. Enabling creates associations but does not switch existing
    IRSA pods: web-identity credentials precede container credentials in the SDK
    chain. A separately reviewed cutover must remove IRSA annotations/injected
    web-identity environment and roll the pods. Both paths use the same role.

    Requires the EKS Pod Identity Agent on the nodes. On Auto Mode clusters it is
    built in and no addon is needed; on a classic node group the
    eks-pod-identity-agent addon would have to be added first.

    Rollback: restore the IRSA annotation and roll pods before setting this back
    to false and applying. Removing associations alone can break pods already
    using container credentials. This flag performs no pod rollout or cutover.
  DESC
  default     = false
}
