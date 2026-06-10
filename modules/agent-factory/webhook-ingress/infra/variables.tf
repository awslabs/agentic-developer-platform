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
  description = "SQS visibility timeout in seconds. Should match pod activeDeadlineSeconds so a killed pod's message becomes visible for retry promptly. Too long = stuck-message pain when pods die; too short = live pods racing re-delivery."
  type        = number
  default     = 21600 # 6h — matches pod activeDeadlineSeconds (see scaledjob.tf). Bumped from 90min so a long-running deploy orchestrator can finish in ONE pod (no cross-pod self-handoff, which is fragile). Max allowed by SQS is 12h.
}

variable "sqs_max_receive_count" {
  description = "Max SQS receive attempts before message is sent to DLQ."
  type        = number
  default     = 3
}

variable "sqs_message_retention" {
  description = "SQS message retention in seconds"
  type        = number
  default     = 345600 # 4 days
}

variable "rate_limit_per_window" {
  description = "Max webhook dispatches per 5-min window per tenant. Bump in tfvars to drain a backlog without code change. Default 50 = original behavior."
  type        = number
  default     = 50000
}

variable "rate_limit_per_hour" {
  description = "Max webhook dispatches per rolling hour per tenant. Bump in tfvars to drain a backlog. Default 500 = original behavior."
  type        = number
  default     = 50000
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
  description = "S3 bucket where the Package Lambda Code CI job uploads zipped Lambda artifacts. Terraform reads the zip from here on apply; the Update Lambda Function Code job is the authoritative code publisher on each deploy. Set via TF_VAR_lambda_artifact_bucket in the workflow."
  type        = string
  default     = ""
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

variable "internal_api_key_arn" {
  description = "Secrets Manager ARN for the X-Internal-Api-Key shared secret used to call /internal/v1/* endpoints on the gateway."
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

# keda_operator_role_name removed — KEDA operator role is now owned by this
# module (keda.tf) rather than discovered via data source. See issue #1052.

variable "agent_image" {
  description = "Container image for the agent worker (ECR URI with tag). Built by .github/workflows/agent-worker-image.yml via CodeBuild → ECR repo adp-agent-runtime."
  type        = string
  default     = ""
}

variable "agent_pod_deadline_seconds" {
  description = "Max runtime for an agent pod before Kubernetes kills it. MUST match sqs_visibility_timeout so a killed pod releases its SQS message for retry at the same instant."
  type        = number
  default     = 21600 # 6h — matches sqs_visibility_timeout. Bumped from 90min so a long-running deploy orchestrator (which dispatches + `gh run watch`es each pipeline phase end-to-end) completes in a SINGLE pod, avoiding fragile cross-pod self-handoff.
}

variable "agent_warm_pool_replicas" {
  description = <<-DESC
    Number of warm-pool "balloon" pods for the agent worker (see warm-pool.tf).
    Each balloon runs the agent image at NEGATIVE priority on its own node, so a
    summoned agent (priority 0) preempts it and lands on an already-running,
    image-pre-pulled node instead of waiting ~60-90s for a cold node + ~30s image
    pull. Set to the number of agents you want able to start instantly in
    parallel. 0 disables the warm pool (scale-from-zero; first agent is slow).
    Each replica holds one node 24/7 at the agent's resource request — the cost
    trade-off for instant starts.
  DESC
  type        = number
  default     = 1
}

variable "agent_image_prepull_enabled" {
  description = <<-DESC
    Run a DaemonSet (warm-pool.tf) that pre-pulls the ~650 MB agent image onto
    EVERY node in adp-agents, so a summoned agent skips the ~30s image pull no
    matter which node it lands on — covering parallel agents beyond the warm-pool
    replica count and agents scheduled onto existing/other nodes. Mirrors the
    proven embark1 `chat-agent-image-prepull` DaemonSet. Pairs with the warm pool
    (which handles the cold-node BOOT delay); together they match embark1's fast
    starts. Tiny footprint (10m cpu / 32Mi per node, just holds the image cached).
  DESC
  type        = bool
  default     = true
}

# Issue #575: the gateway's API Gateway invoke URL is resolved at apply time
# from SSM (published by modules/gateway/infra/) rather than passed in as a
# tfvar. Keeps new environments repeatable — no per-env hardcoding.
