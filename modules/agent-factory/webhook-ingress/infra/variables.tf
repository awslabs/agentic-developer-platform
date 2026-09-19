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

# -----------------------------------------------------------------------------
# Independent Codex SDK pull-request reviewer
# -----------------------------------------------------------------------------

variable "codex_reviewer_enabled" {
  description = "Route eligible pull_request events to the independent Codex SDK reviewer queue."
  type        = bool
  default     = false
}

variable "codex_reviewer_image" {
  description = "Standalone agent-codex-reviewer image. Empty selects adp-codex-reviewer:latest in this account."
  type        = string
  default     = ""
}

variable "codex_reviewer_model" {
  description = "Gateway model identifier used by the independent Codex reviewer."
  type        = string
  default     = "openai.gpt-5.6-sol"
}

variable "codex_reviewer_apply_fixes" {
  description = "Allow Codex to make bounded mechanical fixes before the controller pushes them to the PR branch."
  type        = bool
  default     = true
}

variable "codex_reviewer_merge_enabled" {
  description = "Allow the deterministic Codex reviewer controller to merge a current, approved, green PR."
  type        = bool
  default     = false
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

# Issue #4047 (#2724 slice C)
variable "installation_negative_cache_ttl_seconds" {
  description = "TTL (seconds) for negative-cache rows recording that the gateway authoritatively does not know an installation (identity_type=github_installation_negative in the identity-index, expired via that table's existing `ttl` attribute). Keep this short: it bounds how long a legitimate brand-new tenant can wait before their webhooks resolve. Set to 0 to disable the cache entirely (every unknown installation re-asks the gateway). No new IAM is needed — the Lambda already holds GetItem/PutItem on the identity-index."
  type        = number
  default     = 300

  validation {
    condition     = var.installation_negative_cache_ttl_seconds >= 0 && var.installation_negative_cache_ttl_seconds <= 3600
    error_message = "installation_negative_cache_ttl_seconds must be between 0 (disabled) and 3600. A longer negative cache delays legitimate tenant onboarding past any plausible install flow."
  }
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

variable "org_tenant_auto_create" {
  description = <<-EOT
    Issue #2724: open-onboarding switch for the webhook auto-register tenant gate.
    When false (default), a GitHub org that installs the App is only auto-registered
    as an ADP tenant if an operator or an authenticated ADP flow onboarded it —
    orgs whose only tenant row was self-created by the unauthenticated no-nonce
    install callback are denied (403 unknown_installation, no per-tenant GitHub App
    secret). When true, any installing org becomes a tenant (deliberately-open
    deployments only: hackathons, demos).

    Shares its name with the gateway's ORG_TENANT_AUTO_CREATE
    (modules/gateway/k8s/configmap.yaml) on purpose: one trust decision behind two
    different flag names drifts silently, and this drift is security-relevant. The
    two units govern different halves — the gateway's controls whether the
    unauthenticated install callback may CREATE a shell, this controls whether the
    webhook may TRUST one. Set both true for open onboarding. Gateway true + this
    false is the intended secure default, not a misconfiguration.
  EOT
  type        = bool
  default     = false
}

variable "gh_token_broker_enabled" {
  description = <<-EOT
    Issue #4272: route the agent run's GitHub token through the gateway
    gatekeeper (POST /internal/v1/github-installation-token) instead of minting
    it in-pod from the platform GitHub App private key.

    When false (default), behavior is unchanged: the worker reads
    adp/<env>/tenants/<tenant>/github-app, mints locally, and exports
    GH_APP_PRIVATE_KEY into the agent subprocess. When true, the key is never
    read in this pod at all — neither the bootstrap token nor any in-run refresh
    — and GH_APP_PRIVATE_KEY is not exported. The gateway mints a token scoped
    to the run's own installation and the single repo it was assigned, after
    confirming the installation belongs to the run's tenant.

    Turn this on only after the gatekeeper is verified end-to-end in the target
    environment: there is deliberately NO in-pod fallback, so a gateway that
    cannot mint fails the run loudly at bootstrap rather than degrading quietly.

    ROLLBACK IS A DEPLOY, NOT A TOGGLE. The value is rendered into the KEDA
    ScaledJob pod env by this Terraform, so flipping it back costs a
    webhook-ingress apply (minutes). Pods already running keep the setting they
    started with for the life of the run.
  EOT
  type        = bool
  default     = false
}

variable "require_signed_provenance" {
  description = <<-EOT
    Issue #4128 (#4073 findings #18 + #1b): strict-rejection switch for
    provenance on the /agent/trigger plane.

    A FORGED signature (verify_marker -> False) is ALWAYS rejected with 403,
    regardless of this flag — no legitimate caller sends a bad signature, so
    there is no rollout risk in rejecting one.

    This flag governs only the INDETERMINATE case (verify_marker -> None:
    unsigned marker, no marker at all, or no usable signing key because the
    secret still holds the un-rotated placeholder). When false (default), such
    a request is accepted with the claim's authority stripped and a warning
    logged. When true, it is rejected with 403.

    Default false per #4073's adopted decision 5, and for a concrete reason:
    the in-repo trigger client (agent_worker's adp_trigger/client.py) sends no
    signature today, so flipping this on before that client signs would break
    every legitimate agent-to-agent hop. Land code first, observe the
    "accepting (flag off)" warnings in CloudWatch to confirm no legitimate
    caller is unsigned, then flip. This is deliberately NOT the
    ALLOW_OPEN_SIGNUP pattern where code and config landed out of step and took
    login down.
  EOT
  type        = bool
  default     = false
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
# Persona/model invocability probe (PMM-03 / #5420)
# -----------------------------------------------------------------------------

variable "persona_model_probe_enabled" {
  description = "Enable the scheduled Claude Agent SDK invocability probe. Ships false; PMM-09 may enable it only after the destination account and spend ceiling are approved."
  type        = bool
  default     = false
}

variable "persona_model_probe_schedule" {
  description = "UTC cron schedule for the server-side invocability-probe tick. The CronJob is suspended while persona_model_probe_enabled is false."
  type        = string
  default     = "17 2 * * *"

  validation {
    condition     = length(trimspace(var.persona_model_probe_schedule)) > 0
    error_message = "persona_model_probe_schedule must be a non-empty Kubernetes CronJob schedule."
  }
}

variable "persona_model_probe_deadline_seconds" {
  description = "Hard wall-clock deadline for one scheduled probe tick."
  type        = number
  default     = 180

  validation {
    condition     = var.persona_model_probe_deadline_seconds >= 30 && var.persona_model_probe_deadline_seconds <= 600
    error_message = "persona_model_probe_deadline_seconds must be between 30 and 600 seconds."
  }
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

# -----------------------------------------------------------------------------
# Nightly security agent root dispatch (Issue #4450 / design note #4559)
# -----------------------------------------------------------------------------
# There is deliberately NO `eventbridge_security_agent_persona` variable to match
# `eventbridge_alarm_persona` above. The persona is a literal in the rule's
# InputTransformer and a single entry in the identity row's `allowed_personas`, so
# which persona a machine trigger can spawn is a Terraform-reviewed decision
# rather than a runtime input (#4559 §2.1). A variable here would widen that back.

variable "enable_eventbridge_security_agent_rule" {
  description = "Enable the nightly security agent's root-dispatch EventBridge rule. Creates the rule + target that routes the pipeline's one nightly event to the webhook Lambda, plus the service-identity row it fails closed without."
  type        = bool
  default     = false
}

variable "eventbridge_security_agent_repo" {
  description = "GitHub repo (org/repo) the nightly security agent is dispatched against. A Terraform literal in the rule's InputTransformer — never caller-supplied, because it is the org-gate anchor."
  type        = string
  default     = ""
}

variable "eventbridge_security_agent_org" {
  description = "GitHub org that owns the pipeline repo. Becomes tenant_id AND org_id on the service-identity row; must be a real org with the GitHub App installed, or dispatch fails 422 no_installation_for_tenant."
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# GitLab Webhook (Issue #3324)
# -----------------------------------------------------------------------------

variable "gitlab_webhook_enabled" {
  description = "Enable the GitLab webhook Lambda and API Gateway route. When false, no GitLab resources are created."
  type        = bool
  default     = false
}

# -----------------------------------------------------------------------------
# Adversarial E2E (Issue #3488)
# -----------------------------------------------------------------------------

variable "enable_adversarial_e2e" {
  description = "Enable adversarial E2E test infrastructure (SSM mirror of gateway internal API key, evidence S3 bucket). Requires the secret adp/<env>/gateway/internal-api-key to exist in Secrets Manager. Set to false on fresh deploys where CI has not yet seeded the secret."
  type        = bool
  default     = false
}

# Issue #575: the gateway's API Gateway invoke URL is resolved at apply time
# from SSM (published by modules/gateway/infra/) rather than passed in as a
# tfvar. Keeps new environments repeatable — no per-env hardcoding.

# -----------------------------------------------------------------------------
# Edge authorisation (resource policy)
# -----------------------------------------------------------------------------
# Both default to empty, which reproduces the previous allow-all policy exactly
# — same JSON, so no stage redeployment is triggered for existing deployments.
# Populate them to restrict at the API Gateway edge; HMAC signature
# verification in the Lambda remains the primary control either way.

variable "github_webhook_source_cidrs" {
  description = "CIDRs permitted to call POST /github, normally GitHub's published `hooks` ranges from https://api.github.com/meta. Include the IPv6 prefixes as well as IPv4 — a v4-only list denies v6 deliveries. Empty (default) means no source restriction."
  type        = list(string)
  default     = []
}

variable "internal_route_source_cidrs" {
  description = "CIDRs permitted to call the IAM-authenticated internal routes (POST /agent/trigger) — normally the NAT EIPs the VPC egresses from, because in-VPC callers reach this REGIONAL API over the internet. aws:SourceVpce is not an option: it requires an execute-api interface endpoint, which serves only PRIVATE APIs. Empty (default) means no source restriction."
  type        = list(string)
  default     = []
}

# -----------------------------------------------------------------------------
# Webhook Lambda VPC attachment (optional)
# -----------------------------------------------------------------------------
# Both unset by default, and with no SSM parameters present the Lambda runs
# outside any VPC and reaches the gateway over the public internet — the existing
# behaviour. These variables are the explicit override; the normal source is
# /adp/<env>/webhook-ingress/vpc-config/ (see lambdas.tf for why).
#
# Setting either places the Lambda in private subnets. The reason to want that is that the
# Lambda calls the gateway's API Gateway (GATEWAY_API_URL, e.g.
# /internal/v1/resolve-user). Attached to the VPC with an `execute-api` interface
# endpoint present, that call resolves to the endpoint and arrives with an
# `aws:SourceVpce` context key — which lets the gateway API's resource policy be
# written in terms of the endpoint instead of leaving a public door open for this
# one caller.
#
# Two consequences worth knowing before setting them:
#   - A VPC-attached Lambda loses default internet egress. Anything public it
#     calls (the GitHub API, Secrets Manager and DynamoDB without their own
#     endpoints) then needs a NAT route from the chosen subnets. Use private
#     subnets with a NAT route, not isolated ones.
#   - First invocations after attachment pay ENI setup. Hyperplane ENIs make this
#     far cheaper than it once was, but watch the first few deliveries.

variable "webhook_lambda_subnet_ids" {
  description = "Override for the private subnet ids to place the GitHub webhook Lambda in. Normally left empty and resolved from /adp/<env>/webhook-ingress/vpc-config/subnet-ids instead. Empty with no SSM parameter leaves the Lambda outside the VPC. Subnets must have a NAT route — a VPC-attached Lambda has no default internet egress."
  type        = list(string)
  default     = []
}

variable "webhook_lambda_security_group_ids" {
  description = "Override for the security groups of the VPC-attached GitHub webhook Lambda. Normally resolved from /adp/<env>/webhook-ingress/vpc-config/security-group-ids. Must permit egress to the execute-api endpoint (443) and to anything else the handler calls."
  type        = list(string)
  default     = []
}

# -----------------------------------------------------------------------------
# Live run control (Issue #3960)
# -----------------------------------------------------------------------------
# The control channel lets an authorized owner reach INTO a running agent pod.
# Every variable here defaults to the off/closed position, and the rollout
# invariant is that ordinary workloads stay off until each verb's writer and its
# readers are deployed. Enabling the flag on a shared environment before then is
# how a control that appears to pause a run without doing so reaches a user.

variable "agent_control_enabled" {
  description = "Whether agent-worker pods start a live control listener. Strict: the worker acts on the exact string \"true\" and nothing else, so a typo leaves the feature off rather than half-on. Off by default and intended to stay off for ordinary workloads until a verb is actually implemented — this story ships the authenticated path with every verb answering 501. Read INDEPENDENTLY of the gateway's own FEATURE_AGENT_CONTROL_ENABLED: a config change on one side must not be able to start a listener on the other."
  type        = bool
  default     = false
}

variable "agent_authority_enabled" {
  description = "Enable protected dispatch and mandatory pre-repository pod bootstrap. Keep off until the delegated-authority acceptance and writer migration are complete."
  type        = bool
  default     = false
}

variable "agent_authority_worker_image_digests" {
  description = "Approved immutable worker image digests for TokenReview bootstrap; never image tags."
  type        = set(string)
  default     = []
  validation {
    condition     = alltrue([for digest in var.agent_authority_worker_image_digests : can(regex("^sha256:[0-9a-f]{64}$", digest))])
    error_message = "Worker images must be identified by sha256:<64 lowercase hex digits>."
  }
}

variable "agent_control_signing_key_slot" {
  description = "Active gateway Ed25519 key slot. Switch only after every active listener reports the incoming verification key. See the delegated-authority key rotation runbook."
  type        = string
  default     = "primary"
  validation {
    condition     = contains(["primary", "secondary"], var.agent_control_signing_key_slot)
    error_message = "Signing key slot must be primary or secondary."
  }
}

variable "agent_control_publish_both_keys" {
  description = "Publish both key slots during a staged rotation. Set false only after the old signer is gone and its 30-second forwarding window has elapsed."
  type        = bool
  default     = true
}

variable "agent_control_port" {
  description = "TCP port the in-pod control listener binds, and the ONE port the ingress NetworkPolicy admits. Must match the gateway's AGENT_CONTROL_PORT: the gateway pins the port it will dial rather than reading it from the invocation row, so that a rewritten row cannot redirect control traffic at, say, the kubelet. Changing it here without changing it there breaks control with a 409 rather than falling back."
  type        = number
  default     = 8770

  validation {
    # A privileged port would not bind: the pod runs as UID 1001 with all
    # capabilities dropped (see securityContext in scaledjob.tf).
    condition     = var.agent_control_port > 1024 && var.agent_control_port < 65536
    error_message = "agent_control_port must be an unprivileged port (1025-65535); agent pods run as non-root with NET_BIND_SERVICE dropped."
  }
}

variable "gateway_namespace" {
  description = "Namespace the Bedrock gateway runs in. Used by the control-listener INGRESS allowlist to select which pods may reach a running agent's control port. Matched via the apiserver-managed `kubernetes.io/metadata.name` label rather than a hand-applied one, so it cannot silently stop matching."
  type        = string
  default     = "adp-gateway"
}

variable "engine_command_verifier_role_arn" {
  description = "IAM role ARN of the engine-command VERIFIER — the gateway orchestration tick, which runs in a different deploy unit with its own Terraform state (issue #4539). Passed in rather than referenced because this module cannot see gateway state. Empty (the default) creates no verifier grant at all: an environment that has not wired the verifier gets a signer and no verifier, which fails closed (every command quarantined with no_verification_key) rather than granting access to a role ARN somebody guessed. Must be a role in THIS account; the key policy names it as one of exactly two decrypt principals, so a wrong value here is a real grant to the wrong role."
  type        = string
  default     = ""

  validation {
    # Shape only — Terraform cannot confirm the role exists in another state. A
    # user ARN or an assumed-role session ARN here would produce a key policy that
    # either fails to apply or grants something unintended, so reject both.
    condition     = var.engine_command_verifier_role_arn == "" || can(regex("^arn:aws:iam::[0-9]{12}:role/.+$", var.engine_command_verifier_role_arn))
    error_message = "engine_command_verifier_role_arn must be empty or a full IAM ROLE arn (arn:aws:iam::<account>:role/<name>) — not a user, not an assumed-role session ARN."
  }
}
