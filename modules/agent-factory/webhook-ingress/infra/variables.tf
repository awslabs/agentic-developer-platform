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

variable "lambda_artifact_bucket" {
  description = "S3 bucket where the Package Lambda Code CI job uploads zipped Lambda artifacts. Terraform reads the zip from here on apply; the Update Lambda Function Code job is the authoritative code publisher on each deploy."
  type        = string
  default     = "adp-terraform-state-879318057152"
}

variable "identity_index_table_name" {
  description = "DynamoDB identity-index table name (managed by gateway infra, read by webhook Lambda). Phase B.1 replaces TENANT_TABLE."
  type        = string
  default     = "adp-dev-identity-index"
}

variable "identity_index_table_arn" {
  description = "DynamoDB identity-index table ARN for IAM policy. Passed from gateway infra outputs."
  type        = string
  default     = ""
}

variable "gateway_api_url" {
  description = "Internal Gateway API URL for auto-provisioning calls. Empty disables auto-provision."
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# EKS / KEDA ScaledJob
# -----------------------------------------------------------------------------

variable "eks_cluster_name" {
  description = "EKS cluster name for deploying the agent ScaledJob. The OIDC provider, issuer, and KEDA operator role are discovered from the cluster — no remote-state reads needed."
  type        = string
  default     = "adp-dev-eks-cluster"
}

variable "keda_operator_role_name" {
  description = "Name of the KEDA operator IAM role (discovered as `data.aws_iam_role`). Override if the KEDA Helm release used a non-standard role name."
  type        = string
  default     = "adp-dev-keda-operator-role"
}

variable "agent_image" {
  description = "Container image for the agent worker (ECR URI with tag). Built by .github/workflows/agent-worker-image.yml via CodeBuild → ECR repo adp-agent-runtime."
  type        = string
  default     = "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-agent-runtime:latest"
}

variable "agent_pod_deadline_seconds" {
  description = "Max runtime for an agent pod before Kubernetes kills it"
  type        = number
  default     = 7200 # 2 hours — matches SQS visibility timeout
}
