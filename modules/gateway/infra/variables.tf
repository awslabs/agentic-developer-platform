# =============================================================================
# Gateway Module Variables
# =============================================================================
# Only gateway-specific configuration. VPC, EKS, ECR, and base IAM are
# provided by the shared platform via terraform_remote_state.
# =============================================================================

# Environment Configuration
variable "environment" {
  type        = string
  description = "Environment name: dev, test, or prod"
  validation {
    condition     = contains(["dev", "test", "prod"], var.environment)
    error_message = "Environment must be dev, test, or prod."
  }
}

variable "aws_region" {
  type        = string
  description = "AWS region for resources"
  default     = "us-east-1"
}

# Update mode recovers these from the installed layer versions. Fresh deploys
# continue to use the mutable CodeBuild upload keys; a prior release's
# immutable package and retention setting must never be silently reverted.
variable "pyjwt_layer_s3_key" {
  type    = string
  default = "lambda-layers/pyjwt-py313.zip"
}

variable "pyjwt_layer_skip_destroy" {
  type    = bool
  default = false
}

variable "psycopg2_layer_s3_key" {
  type    = string
  default = "lambda-layers/psycopg2-py312.zip"
}

variable "psycopg2_layer_skip_destroy" {
  type    = bool
  default = false
}

variable "cost_center" {
  type        = string
  description = "Cost center for billing allocation"
}

# =============================================================================
# RDS Configuration
# =============================================================================

variable "rds_instance_class" {
  type        = string
  description = "RDS instance class"
  default     = "db.t4g.medium"
}

variable "rds_allocated_storage" {
  type        = number
  description = "Initial allocated storage for RDS in GB"
  default     = 100
}

variable "rds_max_allocated_storage" {
  type        = number
  description = "Maximum allocated storage for RDS in GB"
  default     = 1000
}

variable "rds_multi_az" {
  type        = bool
  description = "Enable RDS Multi-AZ deployment"
  default     = false
}

variable "rds_backup_retention_period" {
  type        = number
  description = "RDS backup retention period in days"
  default     = 7
}

variable "rds_backup_window" {
  type        = string
  description = "RDS backup window"
  default     = "03:00-04:00"
}

variable "rds_maintenance_window" {
  type        = string
  description = "RDS maintenance window"
  default     = "sun:04:00-sun:05:00"
}

variable "rds_db_name" {
  type        = string
  description = "RDS database name"
  default     = "bedrockgateway"
}

variable "rds_username" {
  type        = string
  description = "RDS master username"
  default     = "bgadmin"
}

variable "enable_rds_iam_auth" {
  type        = bool
  description = "Enable RDS IAM authentication policy on the gateway IRSA role"
  default     = true
}

# =============================================================================
# Redis Configuration
# =============================================================================

variable "enable_redis" {
  type        = bool
  description = "Enable ElastiCache Redis cluster"
  default     = true
}

variable "redis_node_type" {
  type        = string
  description = "ElastiCache Redis node type"
  default     = "cache.t3.micro"
}

variable "redis_num_cache_nodes" {
  type        = number
  description = "Number of cache nodes for Redis cluster"
  default     = 1
}

variable "redis_parameter_group_name" {
  type        = string
  description = "Redis parameter group name"
  default     = "default.redis7"
}

variable "redis_port" {
  type        = number
  description = "Redis port"
  default     = 6379
}

variable "enable_elasticache_iam_auth" {
  type        = bool
  description = "Enable ElastiCache IAM authentication policy on the gateway IRSA role"
  default     = true
}

# =============================================================================
# IAM Configuration
# =============================================================================

variable "pool_account_arns" {
  type        = list(string)
  description = "List of AWS account ARNs that contain Bedrock pools for cross-account access"
  default     = []
}

# =============================================================================
# Cognito Configuration
# =============================================================================

variable "cognito_mfa_configuration" {
  type        = string
  description = "Cognito MFA configuration: OFF (default), ON (required), or OPTIONAL (user-elected)."
  # The GitHub auth broker cannot complete Cognito MFA challenges.
  # Keep both module defaults OFF; enable MFA explicitly after validating sign-in flows.
  default = "OFF"

  validation {
    condition     = contains(["OFF", "ON", "OPTIONAL"], var.cognito_mfa_configuration)
    error_message = "MFA configuration must be OFF, ON, or OPTIONAL."
  }
}

variable "cognito_threat_protection_mode" {
  type        = string
  description = "Cognito threat protection (advanced security) mode: OFF, AUDIT (detect + log risk, no enforcement) or ENFORCED (block/challenge risky sign-ins). AUDIT and ENFORCED require the Cognito Plus feature plan, billed per monthly active user."

  # #5666 (A11): threat protection had no path through this root module at all —
  # opting in would have meant editing module internals. Exposed here so it is a
  # one-line tfvars change per environment.
  #
  # The default MIRRORS the inner module's default deliberately. A wrapper default
  # that disagreed with the module it wraps is the precise defect above: the outer
  # value silently wins and the inner hardening becomes dead code. Keeping the two
  # equal means this pass-through cannot shadow anything.
  #
  # OFF, unlike the other defaults in this issue, because the Plus plan is a
  # per-monthly-active-user charge: a merge must not change an AWS bill. AUDIT is
  # the recommended first step — it logs risk without altering any sign-in outcome.
  default = "OFF"

  validation {
    condition     = contains(["OFF", "AUDIT", "ENFORCED"], var.cognito_threat_protection_mode)
    error_message = "cognito_threat_protection_mode must be OFF, AUDIT, or ENFORCED."
  }
}

variable "cognito_callback_urls" {
  type        = list(string)
  description = "List of allowed callback URLs for the Cognito User Pool client (OAuth 2.0 redirect URIs)"
  default     = ["http://localhost:5173/auth/callback"]
}

variable "cognito_logout_urls" {
  type        = list(string)
  description = "List of allowed logout URLs for the Cognito User Pool client"
  default     = ["http://localhost:5173"]
}

variable "cognito_custom_domain" {
  type        = string
  description = "Custom domain for the Cognito User Pool (optional). If not provided, uses Cognito hosted domain."
  default     = ""
}

variable "cognito_custom_domain_certificate_arn" {
  type        = string
  description = "ACM certificate ARN (us-east-1) for the Cognito custom domain. Required when cognito_custom_domain is an FQDN; empty by default."
  default     = ""
}

variable "cognito_access_token_validity" {
  type        = number
  description = "Access token validity in minutes (default: 60 = 1 hour)"
  default     = 60
}

variable "cognito_refresh_token_validity" {
  type        = number
  description = "Refresh token validity in minutes (default: 43200 = 30 days)"
  default     = 43200
}

variable "cognito_cli_refresh_token_validity" {
  type        = number
  description = "Refresh token validity in minutes for the CLI app client (default: 1440 = 24 hours). Short by design — this is the credential that sits on developer laptops."
  default     = 1440
}

variable "cognito_id_token_validity" {
  type        = number
  description = "ID token validity in minutes (default: 60 = 1 hour)"
  default     = 60
}

# =============================================================================
# GitHub OAuth Identity Provider (Issue #313)
# =============================================================================

variable "enable_github_oauth" {
  type        = bool
  description = "Enable GitHub as a federated identity provider in Cognito via OAuth/OIDC"
  default     = false
}

variable "github_oauth_client_id" {
  type        = string
  description = "GitHub OAuth App client ID. Required when enable_github_oauth is true."
  default     = ""
}

variable "github_oauth_client_secret" {
  type        = string
  description = "GitHub OAuth App client secret. Required when enable_github_oauth is true."
  default     = ""
  sensitive   = true
}

# =============================================================================
# Task API Submission Route (Issue #5795, T2)
# =============================================================================
# The Lambda that serves POST /v1/tasks is owned by
# modules/agent-factory/webhook-ingress, a separate Terraform state, so its
# identifiers come in as variables the way internal_alb_arn does.
#
# Two independent switches, deliberately: this route can EXIST without
# ACCEPTING anything. Publishing it is a gateway apply; accepting submissions
# additionally requires ADP_TASK_API_ADMISSION_ENABLED on the Lambda. So the
# edge can be in place and verified before any task is admitted, and admission
# can be withdrawn without a gateway apply.

variable "task_api_lambda_invoke_arn" {
  type        = string
  description = "Invoke ARN of the webhook-ingress Lambda serving POST /v1/tasks. Empty publishes no task route."
  default     = ""
}

variable "task_api_lambda_function_name" {
  type        = string
  description = "Function name of the webhook-ingress Lambda serving POST /v1/tasks (for the scoped aws_lambda_permission)."
  default     = ""
}

variable "enable_task_api_route" {
  type        = bool
  description = "Publish the explicit POST /v1/tasks route on the main API Gateway and exact /api/v1/tasks CloudFront route. Default off; publishing the route does not admit any task on its own."
  default     = false
}

# =============================================================================
# GitHub Auth Broker Configuration (Issue #520)
# =============================================================================

variable "enable_github_auth_broker" {
  type        = bool
  description = "Enable the GitHub auth broker Lambda (replaces the failed Cognito-OIDC attempt). Independent of var.enable_github_oauth."
  default     = false
}

variable "github_auth_allowlist_mode" {
  type = string
  # Issue #4844: 'platform' added. It is a mode this variable ACCEPTS, not one any
  # environment is set to — flipping an environment to it is a deliberate operator
  # action after a verified deploy and smoke test, never part of a merge.
  description = "Allowlist mode for GitHub auth broker: 'org' (GitHub org membership), 'platform' (≥1 platform org membership — #4844), 'explicit' (not implemented in the broker; denies), or 'open' (no enforcement — requires github_auth_allow_open_signup). Issue #3986: defaults to 'org' so the shipped default fails closed."
  default     = "org"
  validation {
    condition     = contains(["org", "platform", "explicit", "open"], var.github_auth_allowlist_mode)
    error_message = "Allowlist mode must be 'org', 'platform', 'explicit', or 'open'."
  }
  # Cross-variable checks are gated on enable_github_auth_broker so that
  # deployments with the broker disabled (the default) are unaffected by the
  # fail-closed defaults.
  validation {
    condition     = !var.enable_github_auth_broker || var.github_auth_allowlist_mode != "org" || trimspace(var.github_auth_allowed_orgs) != ""
    error_message = "github_auth_allowed_orgs must be set when github_auth_allowlist_mode is 'org' (an empty org list denies every sign-in)."
  }
  validation {
    condition     = !var.enable_github_auth_broker || var.github_auth_allowlist_mode != "open" || var.github_auth_allow_open_signup
    error_message = "github_auth_allowlist_mode = 'open' disables allowlist enforcement entirely; set github_auth_allow_open_signup = true to acknowledge this."
  }
  # Issue #4844: no validation is needed to guarantee 'platform' mode has a
  # projection to read. Both identity-index tables are unconditional resources of
  # this root module and their names are passed to both Lambdas unconditionally
  # (see the module "cognito" and module "github_auth_broker" blocks in main.tf),
  # so IDENTITY_INDEX_TABLE cannot be empty in a deployed environment. The
  # Lambda-side guard for an unset table (reader ⇒ UNAVAILABLE ⇒ deny) remains as
  # defence in depth, and is covered by the fail-closed tests.
}

variable "github_auth_allowed_orgs" {
  type        = string
  description = "Comma-separated GitHub orgs for allowlist_mode=org. Required when mode is 'org'."
  default     = ""
}

variable "github_auth_allow_open_signup" {
  type        = bool
  description = "Escape hatch (#3986): honour github_auth_allowlist_mode = 'open', which lets ANY GitHub user provision a Cognito account. Leave false unless open signup is intentional."
  default     = false
}

variable "github_auth_token_secret_arn" {
  type        = string
  description = "Secrets Manager ARN for GitHub API token used in org membership checks. Strongly recommended when allowlist_mode is 'org': without it the broker falls back to the signing-in user's own OAuth token, which cannot verify membership unless the OAuth App is org-approved."
  default     = ""
}

# =============================================================================
# Frontend Configuration
# =============================================================================

variable "frontend_domain_name" {
  type        = string
  description = "Custom domain name for CloudFront frontend (optional)"
  default     = ""
}

variable "frontend_acm_certificate_arn" {
  type        = string
  description = "ACM certificate ARN for custom domain (required if frontend_domain_name is set)"
  default     = ""
}

variable "cloudfront_waf_web_acl_arn" {
  type        = string
  description = "ARN of a WAFv2 web ACL (scope CLOUDFRONT, created in us-east-1) to associate with the frontend distribution. Empty string (the default) leaves the distribution unassociated, which is the prior behaviour."
  default     = ""
}

variable "enable_broker_cloudfront_route" {
  type        = bool
  description = "Serve the GitHub auth broker through CloudFront at /auth/github/* instead of sending the browser to the API Gateway hostname. Additive and inert until VITE_GITHUB_AUTH_BROKER_URL and the broker's CALLBACK_URL are repointed at the distribution, so it can be enabled ahead of the cutover. Requires enable_api_gateway."
  default     = false
}

variable "frontend_additional_connect_src" {
  type        = list(string)
  description = "Extra CSP connect-src sources for the CloudFront response headers policy, e.g. [\"wss://ws.example.com\"] when the agent WebSocket API is fronted by a custom domain. The directive's `https:` source does not cover `wss:`, so without this the browser blocks the chat WebSocket with a console-only CSP error and no Network-tab entry. Empty (default) leaves the policy unchanged."
  default     = []
}

variable "enable_frontend_waf" {
  type        = bool
  description = "Enable WAF web ACL for CloudFront frontend"
  default     = false
}

# =============================================================================
# VPC Origin Configuration (for internal ALB)
# =============================================================================

variable "enable_vpc_origin" {
  type        = bool
  description = "Enable CloudFront VPC Origin for internal ALB access. When true, the ALB should be configured as internal (scheme: internal)."
  default     = false
}

variable "internal_alb_arn" {
  type        = string
  description = "ARN of the internal ALB for CloudFront VPC Origin and API Gateway VPC Link v2. For EKS Ingress-managed ALBs, this is set dynamically in the backend-deploy workflow."
  default     = ""
}

variable "internal_alb_dns" {
  description = "DNS name of the internal ALB. Set dynamically by the deploy workflow after the EKS Ingress ALB is created. (Issue #42)"
  type        = string
  default     = "localhost"
}

variable "alb_security_group_ids" {
  description = "Security group IDs of the internal ALB. Used by API Gateway VPC Link v2 SG for egress rules. Set dynamically by the deploy workflow. (Issue #42)"
  type        = list(string)
  default     = []
}

# =============================================================================
# Internal-plane ALB (Issue #4010)
# =============================================================================
# The internal control plane (`/internal/{proxy+}`) is served by a separate ALB
# created by modules/gateway/k8s/ingress-internal.yaml, which CloudFront has no
# VPC origin for — making the internal plane unreachable from the edge by
# routing rather than only by header stripping at the CloudFront function.
#
# All three are empty by default, which makes the internal route fall back to
# the edge ALB (exactly pre-#4010 behavior). platform/scripts/wire-gateway-alb.sh
# discovers the internal Ingress's ALB and populates them via TF_VAR_*.
variable "internal_plane_alb_arn" {
  description = "Load balancer ARN (NOT a listener ARN) of the internal-plane ALB. Set dynamically by wire-gateway-alb.sh. Empty falls back to the edge ALB. (Issue #4010)"
  type        = string
  default     = ""
}

variable "internal_plane_alb_dns" {
  description = "DNS name of the internal-plane ALB. Set dynamically by wire-gateway-alb.sh. Empty falls back to the edge ALB. (Issue #4010)"
  type        = string
  default     = ""
}

variable "internal_plane_alb_security_group_ids" {
  description = "Security group IDs of the internal-plane ALB. Used for the VPC Link v2 SG egress + matching ALB ingress rules. Set dynamically by wire-gateway-alb.sh. (Issue #4010)"
  type        = list(string)
  default     = []
}

variable "vpc_origin_read_timeout" {
  type        = number
  description = "Origin read timeout in seconds for VPC Origin. CloudFront caps this at 60s for VPC origins (custom origins allow up to 180s)."
  default     = 60
}

variable "vpc_origin_keepalive_timeout" {
  type        = number
  description = "Origin keepalive timeout in seconds for API requests."
  default     = 60
}

# =============================================================================
# GitLab VPC Origin Configuration
# =============================================================================

variable "gitlab_origin_dns" {
  type        = string
  description = "DNS hostname of the GitLab internal ALB. When non-empty (along with gitlab_origin_arn), a VPC Origin and /gitlab/* cache behavior are added to CloudFront."
  default     = ""
}

variable "gitlab_origin_arn" {
  type        = string
  description = "ARN of the GitLab internal ALB for VPC Origin. Required together with gitlab_origin_dns."
  default     = ""
}

# =============================================================================
# Chat Logging Configuration (Issue #143)
# =============================================================================

variable "enable_chat_logging" {
  type        = bool
  description = "Enable async chat logging to S3 with PII scrubbing"
  default     = true
}

# --- Orchestration tick (Issue #4203) ---------------------------------------

variable "enable_orchestration_tick" {
  type        = bool
  description = <<-EOT
    Create the scheduled orchestration tick Lambda (EventBridge -> VPC Lambda ->
    RDS). Enabled by default: without the tick nothing advances the delivery-loop
    graph. Requires migration 029_orchestration_graph to be applied.
  EOT
  default     = true
}

variable "orchestration_tick_schedule" {
  type        = string
  description = "EventBridge schedule expression for the orchestration tick"
  default     = "rate(5 minutes)"
}

variable "orchestration_tick_image_tag" {
  type        = string
  description = <<-EOT
    Tag of the adp-gateway image the tick Lambda runs. The tick's logic lives in
    `src/orchestration/tick.py`, which ships inside the gateway image, so it uses
    the same artifact the pod does.
  EOT
  default     = "latest"
}

variable "orchestration_tick_image_digest" {
  type        = string
  default     = null
  description = "Optional immutable gateway image digest. Overrides the tag during staged upgrades."
  validation {
    condition     = var.orchestration_tick_image_digest == null ? true : can(regex("^sha256:[0-9a-f]{64}$", var.orchestration_tick_image_digest))
    error_message = "The orchestration image digest must be sha256 followed by 64 lowercase hexadecimal characters."
  }
}

variable "orchestration_alert_email_addresses" {
  type        = list(string)
  description = <<-EOT
    Email addresses notified when the engine detects a stalled or halted node
    (Issue #4211). Each address must be confirmed by its owner before AWS delivers
    to it, so populating this is apply-then-confirm rather than apply-only.

    Left empty the topic still exists and publishes still succeed — the alert simply
    reaches nobody. That state is intentionally visible rather than fatal: the tick
    reports `StallsDetected` and `NotificationsFailed` separately, so an
    unsubscribed topic shows up as detections with no delivery instead of as a
    healthy-looking silence.
  EOT
  default     = []
}

# -----------------------------------------------------------------------------
# Engine dispatch (Issue #4313 — ruling docs/design-notes/4303-engine-genesis-transport.md)
# -----------------------------------------------------------------------------
# The orchestration tick resolves the gate approver in-process and produces the
# agent envelope directly onto the existing agent-submit FIFO queue. The queue is
# owned by modules/agent-factory/webhook-ingress/infra/ — a different Terraform
# state — so its ARN and URL come in as variables, following the
# `agent_context_ingestion_queue_arn` pattern above. Do not hardcode either.

variable "orchestration_dispatch_queue_arn" {
  type        = string
  description = <<-EOT
    ARN of the agent-submit FIFO queue the engine dispatches onto. Scopes the
    tick's `sqs:SendMessage` grant.

    Empty falls back to the conventional `adp-<env>-agent-submit.fifo` name inside
    the tick module, so the policy stays SCOPED even when this is unset — it never
    degrades to `Resource = "*"`.
  EOT
  default     = ""
}

variable "orchestration_dispatch_queue_url" {
  type        = string
  description = <<-EOT
    URL of the agent-submit FIFO queue, passed to the tick as
    BG_ORCH_DISPATCH_QUEUE_URL. Read from
    /adp/<env>/webhook-ingress/sqs-queue-url (written by the webhook-ingress
    module's outputs.tf) or supplied per environment.

    Empty is the default and is safe: nothing is dispatched, and the tick reports
    `dispatch_enabled=false` so the unwired state is visible rather than reading as
    an idle engine.
  EOT
  default     = ""
}

variable "orchestration_dispatch_repo" {
  type        = string
  description = <<-EOT
    `owner/name` of the repository the engine dispatches delivery-loop work into.

    Required for dispatch because the orchestration graph does not carry one:
    `OrchestrationNode` stores only `issue_ref` (an issue number), while the agent
    worker requires `source_ref.{installation_id, repo, issue}`. Empty means no
    dispatch happens.
  EOT
  default     = ""
  validation {
    condition     = var.orchestration_dispatch_repo == "" || can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.orchestration_dispatch_repo))
    error_message = "orchestration_dispatch_repo must be empty or an owner/repository name."
  }
}

variable "orchestration_dispatch_max_per_tick" {
  type        = number
  description = <<-EOT
    Maximum dispatches per tick. Bounds the one unbounded surface in engine
    dispatch: every dispatch is agent capacity and model spend, so an uncapped pass
    over a large flow is a cost spike. The cap delays work rather than dropping it.
  EOT
  default     = 10
}

# -----------------------------------------------------------------------------
# GitHub engine-command bridge (Issue #4527)
# -----------------------------------------------------------------------------
# The tick consumes `@agent-engine` comments that the webhook Lambda marked on the
# webhook-events row. The table, its KMS key and the per-tenant GitHub App secrets
# are owned by other Terraform states, so all three arrive as variables — the same
# pattern as the dispatch queue above. Every one defaults to empty, which leaves
# the bridge inert rather than half-wired.

variable "orchestration_engine_enabled" {
  type        = bool
  description = <<-EOT
    Enable the orchestration engine for this environment. Enabled by default
    for new deployments; set false to retain GitHub-only operation. Keep the
    gateway SSM feature-orchestration-engine override aligned with this value.
    Runtime consumers require an explicit true value and reject malformed flags.
  EOT
  default     = true
}

variable "orchestration_webhook_events_table" {
  type        = string
  description = <<-EOT
    Name of the webhook-events DynamoDB table the tick Queries for outstanding
    `@agent-engine` commands (WEBHOOK_EVENTS_TABLE). Conventionally
    `adp-<env>-webhook-events`; owned by the webhook-ingress state.

    Empty is the default and is safe: the pass reports `commands_enabled=false`, so
    an unwired bridge is visible rather than reading as "nobody has commented".
  EOT
  default     = ""
}

variable "orchestration_agent_authority_enabled" {
  description = "Enable protected engine dispatch together with worker/gateway authority migration. Requires the webhook events table and KMS key inputs."
  type        = bool
  default     = false
}

variable "orchestration_webhook_events_kms_key_arn" {
  type        = string
  description = <<-EOT
    ARN of the KMS key encrypting the webhook-events table. Required for the tick's
    Query and UpdateItem to succeed at runtime — a missing grant here fails at call
    time, not at plan time.
  EOT
  default     = ""
}

variable "orchestration_github_app_secret_arn_pattern" {
  type        = string
  description = <<-EOT
    Secrets Manager ARN pattern for per-tenant GitHub App credentials
    (`adp/<env>/tenants/*/github-app`), used ONLY to mint an installation token for
    a command acknowledgement comment.

    A pattern because tenants are created at runtime. Empty means the tick applies
    commands but cannot acknowledge them, which it reports as
    `command_acks_failed` — a non-success tick rather than a silent one.
  EOT
  default     = ""
}

variable "orchestration_engine_command_signing_key_secret_arn" {
  type        = string
  description = <<-EOT
    Issue #4539. Secrets Manager ARN of the engine-command attribution signing
    keyring, which the tick uses to VERIFY that a pending command's authority and
    routing fields were delivered by GitHub rather than authored by whatever could
    write the row.

    Created by the webhook-ingress state (the signer), which publishes it as the
    `engine_command_signing_key_secret_arn` output and to SSM at
    `/adp/<env>/webhook-ingress/engine-command-signing-key-arn`. Only the ARN
    crosses the state boundary; the key value is seeded out of band per
    `docs/runbooks/engine-command-signing-key-rotation.md`.

    Empty is the fail-closed default: the verifier reports `no_verification_key` and
    every command quarantines rather than being applied unverified.

    Pair this with the reverse direction — pass the `orchestration_tick_role_arn`
    output of this state to the webhook-ingress state's
    `engine_command_verifier_role_arn`, which is what grants the tick read access to
    the secret and decrypt on its dedicated CMK. Integration owned by #5195/#5210.
  EOT
  default     = ""
}

variable "chat_logging_scrub_level" {
  type        = string
  description = "Chat logging scrub level: off, basic (headers+regex), or standard (headers+regex+Comprehend PII)"
  default     = "standard"
  validation {
    condition     = contains(["off", "basic", "standard"], var.chat_logging_scrub_level)
    error_message = "Chat logging scrub level must be off, basic, or standard."
  }
}

variable "chat_logging_kms_key_arn" {
  type        = string
  description = "KMS key ARN for S3 SSE-KMS encryption of chat logs. If empty, uses SSE-S3 (AES256)."
  default     = ""
}

# =============================================================================
# Distributed Tracing Configuration (Issue #144)
# =============================================================================

variable "enable_xray_tracing" {
  type        = bool
  description = "Enable X-Ray tracing IAM permissions for the gateway service role."
  default     = false
}

# Issue #2709: bedrock-mantle passthrough for OpenAI Responses-API traffic.
# When enabled, grants the gateway pod's IRSA role direct bedrock:InvokeModel*
# so it can SigV4-sign requests to the mantle/OpenAI endpoint with its own
# credentials (the Claude proxy path uses cross-account assume-role instead).
variable "enable_mantle_passthrough" {
  type        = bool
  description = "Enable bedrock:InvokeModel* IAM permissions for the bedrock-mantle OpenAI passthrough (Issue #2709)."
  default     = false
}

# =============================================================================
# CloudFront Access Logging
# =============================================================================

variable "enable_cloudfront_logging" {
  type        = bool
  description = "Enable CloudFront standard access logging to S3."
  default     = false
}

variable "cloudfront_log_retention_days" {
  type        = number
  description = "Number of days to retain CloudFront access logs in S3"
  default     = 90
}

# =============================================================================
# CloudWatch Latency Dashboard (Issue #144)
# =============================================================================

variable "alb_arn_suffix" {
  type        = string
  description = "ALB ARN suffix for CloudWatch metrics (format: app/<name>/<id>). Set after the EKS Ingress ALB is created. Leave empty if ALB not yet provisioned."
  default     = ""
}

# =============================================================================
# API Gateway Configuration (Issue #236)
# =============================================================================

variable "agent_route_source_cidrs" {
  type        = list(string)
  description = "CIDRs permitted to call the gateway API's /agent and /agent/* routes — normally the NAT EIPs, since agent workers reach this REGIONAL API over the internet. Empty (default) creates no resource policy. See docs/security/eaa-deployment-runbook.md 5.1."
  default     = []
}

variable "internal_route_source_cidrs" {
  type        = list(string)
  description = "CIDRs permitted to call the gateway API's /internal/* routes — normally the NAT EIPs. Busiest path on the API; a stale value fails agent invocation at identity resolution, which looks like a GitHub or tenant fault. Empty (default) creates no resource policy."
  default     = []
}

variable "enable_api_gateway" {
  type        = bool
  description = "Enable API Gateway REST API as alternate route to ALB (with streaming support)"
  default     = false
}

variable "api_gateway_throttle_burst_limit" {
  type        = number
  description = "API Gateway throttling burst limit (requests per second)"
  default     = 100
}

variable "api_gateway_throttle_rate_limit" {
  type        = number
  description = "API Gateway throttling rate limit (requests per second)"
  default     = 50
}

variable "api_gateway_log_retention_days" {
  type        = number
  description = "CloudWatch API Gateway access-log retention in days (minimum 365)"
  default     = 365

  validation {
    condition     = contains([365, 400, 545, 731, 1827, 3653], var.api_gateway_log_retention_days)
    error_message = "API Gateway access-log retention must be a valid CloudWatch value of at least 365 days."
  }
}

variable "authorizer_ip_allowlist_ssm_parameter" {
  type        = string
  description = "SSM String parameter name holding a comma-separated CIDR allowlist for the API authorizer's JWT/browser path. Empty by default (no restriction)."
  default     = ""
}

# =============================================================================
# Lambda Reserved Concurrency (Issue #2910)
# =============================================================================

variable "enable_lambda_reserved_concurrency" {
  type        = bool
  description = "Enable reserved concurrent executions on gateway Lambda functions. Set to false on fresh accounts where the Lambda concurrency quota is too low (sum of reservations must leave >= 100 unreserved)."
  default     = true
}

# =============================================================================
# Agent Context Ingestion (Issue #1797)
# =============================================================================

variable "enable_agent_context_sqs" {
  type        = bool
  description = "Enable SQS SendMessage permission on gateway IRSA role for Phase 1 inline ingestion dispatch."
  default     = false
}

variable "agent_context_ingestion_queue_arn" {
  type        = string
  description = "ARN of the agent-context SQS ingestion queue. Empty derives the standard queue in the deployment account and region."
  default     = ""
}


# =============================================================================
# Budget Enforcement Alarms (Issue #4075)
# =============================================================================

variable "budget_alarm_sns_topic_arns" {
  type        = list(string)
  description = "SNS topic ARNs notified by budget alarms. When empty, pricing alarms use a dedicated encrypted SNS topic with an SQS operational inbox; other budget-enforcement alarms remain console-only."
  default     = []
}

variable "pricing_refresh_timeout" {
  description = "Pricing refresh Lambda timeout in seconds; includes bounded source fetches, publication and metrics."
  type        = number
  default     = 180

  validation {
    condition     = var.pricing_refresh_timeout >= 180 && var.pricing_refresh_timeout <= 900
    error_message = "Pricing refresh needs at least 180 seconds for its 120-second source deadline and publication; Lambda permits at most 900."
  }
}

# =============================================================================
# Test Users Configuration (Issue #60)
# =============================================================================

variable "create_test_users" {
  type        = bool
  description = "Create Cognito test users (admins group, test user, test admin) with Secrets Manager credentials. For dev/test only — never enable in production."
  default     = false
}

variable "cloudfront_enable_ipv6" {
  description = "Publish AAAA records for the frontend distribution. Set false when an IPv4-only tunnel (e.g. a ZTNA client) fronts the distribution and its web ACL allowlists IPv4 addresses only — otherwise IPv6 clients bypass the tunnel and are blocked by the ACL's default action. Default true preserves prior behaviour."
  type        = bool
  default     = true
}

variable "user_identity_index_v2_read" {
  type        = string
  description = "Issue #4849: whether the auth Lambdas' membership-eligibility read tries the v2 user-identity-index table before the legacy one. String, not bool, because it is passed straight through to a Lambda env var. Mirrors the webhook-ingress reader's USER_IDENTITY_INDEX_V2_READ flag (#537) so all readers can be moved together."
  default     = "false"
}

variable "persona_model_mapping_enabled" {
  description = "Use saved persona models at dispatch without changing worker authority."
  type        = bool
  default     = true
}
