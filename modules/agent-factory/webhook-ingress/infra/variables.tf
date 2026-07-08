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
  description = "SQS base visibility timeout in seconds (dead-worker detection window). Decoupled from activeDeadlineSeconds — healthy workers extend visibility via a heartbeat thread (ChangeMessageVisibility every ~120s, extending by ~300s). A dead worker stops heartbeating and its message frees after this timeout. Keep this short (~5min) so stuck messages unblock their FIFO group quickly."
  type        = number
  default     = 300 # 5min — dead-worker detection window. Worker heartbeat extends visibility while alive. Previously 21600 (6h, coupled to activeDeadlineSeconds); decoupled by #2324.
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
# Lambda reserved concurrency (Issue #2928)
# -----------------------------------------------------------------------------

variable "enable_lambda_reserved_concurrency" {
  type        = bool
  description = "Enable reserved concurrent executions on the webhook Lambda. Set to false on accounts where the Lambda concurrency quota is too low (sum of reservations must leave >= 100 unreserved). Mirrors the gateway gate from issue #2910."
  default     = true
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
  description = "Max runtime for an agent pod before Kubernetes kills it. Decoupled from sqs_visibility_timeout (#2324) — the worker heartbeat bridges the gap. This controls the absolute max run time; sqs_visibility_timeout controls dead-worker detection speed."
  type        = number
  default     = 21600 # 6h — max run time for long deploy orchestrators. Independent of sqs_visibility_timeout (300s); worker heartbeat extends visibility while alive.
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

    Default is 0 (disabled). At replicas=1 the pool only warms a SINGLE agent
    into an idle cluster; parallel bursts (the common case) and the 2nd+ agent
    still cold-start while the lone balloon slowly replenishes — so it rarely
    earned the permanent node (1 vCPU + 4Gi + 50Gi disk) it reserved 24/7. The
    image-prepull DaemonSet (agent_image_prepull_enabled) already eliminates the
    ~30s image-pull on every warm node, leaving only the ~60-90s node-provision
    head start for that one first agent. Raise this only if instant first-agent
    starts after idle are worth a standing node.
  DESC
  type        = number
  default     = 0
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

# -----------------------------------------------------------------------------
# Knowledge Layer (Issue #3286)
# -----------------------------------------------------------------------------

variable "knowledge_layer_enabled" {
  description = <<-DESC
    Enable Knowledge Layer access from agent-worker pods. When true, adds
    KNOWLEDGE_LAYER_ENABLED and CONTEXT_MCP_SERVER_URL env vars to the
    ScaledJob agent-worker container, connecting hosted agents to the
    agent-context MCP server for code intelligence tools (search, understand,
    impact, browse, remember, experience, secure). Default true — the
    agent-context service must be deployed for tools to resolve.
  DESC
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# OpenTelemetry / Observability (Issue #1630)
# -----------------------------------------------------------------------------

variable "enable_agent_otel" {
  description = <<-DESC
    Enable OpenTelemetry telemetry export from agent-worker pods. When true:
    - Deploys an ADOT Collector (Deployment + Service) in adp-agents namespace
    - Adds OTEL_* env vars to the ScaledJob agent-worker container
    - Creates an IRSA role scoped to CloudWatch + X-Ray write-only
    Telemetry is fire-and-forget; a misconfigured/down collector does NOT
    block agent runs. Default false — flip after collector is healthy.
  DESC
  type        = bool
  default     = false
}

variable "otel_collector_image" {
  description = "ADOT Collector container image. Use the AWS-maintained public ECR image."
  type        = string
  default     = "public.ecr.aws/aws-observability/aws-otel-collector:v0.40.0"
}

variable "otel_collector_log_group" {
  description = "CloudWatch Logs group for OTEL log pipeline output. Created by the collector itself (awscloudwatchlogs exporter auto-creates)."
  type        = string
  default     = "/adp/dev/agent-factory/otel"
}

# -----------------------------------------------------------------------------
# EventBridge / Machine Triggers (Issue #2154)
# -----------------------------------------------------------------------------

variable "enable_eventbridge_alarm_rule" {
  description = "Enable the example CloudWatch alarm-state-change EventBridge rule. Creates a rule + target that routes ALARM events to the webhook Lambda for agent triage."
  type        = bool
  default     = false
}

variable "eventbridge_alarm_persona" {
  description = "Persona to spawn when a CloudWatch alarm fires (default: operations)."
  type        = string
  default     = "operations"
}

variable "eventbridge_alarm_target_repo" {
  description = "GitHub repo (org/repo) where triage issues are created for alarm events."
  type        = string
  default     = ""
}

# Issue #575: the gateway's API Gateway invoke URL is resolved at apply time
# from SSM (published by modules/gateway/infra/) rather than passed in as a
# tfvar. Keeps new environments repeatable — no per-env hardcoding.
