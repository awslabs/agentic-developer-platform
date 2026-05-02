variable "environment" {
  description = "Deployment environment (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "aws_region" {
  description = "AWS region for resources"
  type        = string
  default     = "us-east-1"
}

variable "tags" {
  description = "Tags to apply to all resources"
  type        = map(string)
  default     = {}
}

# -----------------------------------------------------------------------------
# SQS tuning
# -----------------------------------------------------------------------------

variable "sqs_visibility_timeout" {
  description = "SQS visibility timeout in seconds (should exceed max agent runtime)"
  type        = number
  default     = 7200 # 2 hours — generous for longest-running personas
}

variable "sqs_message_retention" {
  description = "SQS message retention in seconds"
  type        = number
  default     = 345600 # 4 days
}

variable "sqs_max_receive_count" {
  description = "Max SQS receive attempts before message is sent to DLQ"
  type        = number
  default     = 3
}

# -----------------------------------------------------------------------------
# Lambda tuning
# -----------------------------------------------------------------------------

variable "lambda_runtime" {
  description = "Python runtime for webhook Lambdas"
  type        = string
  default     = "python3.12"
}

variable "lambda_memory_size" {
  description = "Lambda memory in MB. 256 is plenty for HMAC + SQS publish."
  type        = number
  default     = 256
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds. Target: <300ms actual; 30s gives headroom for cold-starts."
  type        = number
  default     = 30
}

# -----------------------------------------------------------------------------
# EKS / KEDA ScaledJob
# -----------------------------------------------------------------------------

variable "eks_cluster_name" {
  description = "EKS cluster name for deploying the agent ScaledJob"
  type        = string
  default     = "adp-dev-eks"
}

variable "oidc_provider_arn" {
  description = "ARN of the EKS OIDC provider for IRSA trust policies"
  type        = string
}

variable "oidc_issuer" {
  description = "OIDC issuer URL of the EKS cluster (https://oidc.eks...)"
  type        = string
}

variable "keda_operator_role_arn" {
  description = "ARN of the KEDA operator IAM role (for chain-assume trust)"
  type        = string
}

variable "agent_image" {
  description = "Container image for the agent worker (ECR URI with tag)"
  type        = string
  default     = "adp-agent:latest"
}

variable "agent_pod_deadline_seconds" {
  description = "Max runtime for an agent pod before Kubernetes kills it"
  type        = number
  default     = 7200 # 2 hours — matches SQS visibility timeout
}
