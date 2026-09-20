environment = "dev"
aws_region  = "us-east-1"

# Cost center
cost_center = "engineering"

# Database
rds_instance_class    = "db.t3.medium"
rds_allocated_storage = 20

# Redis
redis_node_type = "cache.t3.micro"

# Cognito
cognito_custom_domain = ""

# API Gateway (REST API with 15-min timeout)
enable_api_gateway = true

# Issue #60: Provision test users for dev environment
create_test_users = true

# Issue #520: enable the GitHub auth broker Lambda (replaces the reverted
# Cognito-OIDC attempt from #518/#519). Reads OAuth App credentials from
# Secrets Manager at adp/dev/cognito/github-oauth-credentials
# (pre-provisioned out-of-band).
enable_github_auth_broker = true

# Issue #3986: the broker allowlist now fails closed (mode defaults to "org" and
# an empty org list denies). Dev previously relied on the implicit "open" default,
# which let ANY GitHub user provision a Cognito account, so these must be set
# explicitly or GitHub login returns not_authorized.
#
# ⚠️ TEMPORARILY "open" until #4139 lands. mode=org is broken while the org
# check runs on the signing-in user's OAuth token: GitHub 404s org membership
# to un-granted OAuth apps and the broker misreads that as DENIED, locking out
# every user including org owners. This fired TWICE (2026-08-24 manual flip;
# 2026-08-26 when gateway-infra-apply run 33017530462 re-applied this pin over
# the hand-patched Lambda env). Do NOT set "org" here again until #4139 (App
# installation-token org check) is deployed; then remove allow_open_signup.
github_auth_allowlist_mode    = "open"
github_auth_allow_open_signup = true
github_auth_allowed_orgs      = "aws-e"

# github_auth_token_secret_arn is intentionally unset: no org-check token secret
# exists in this account yet. The broker falls back to the signing-in user's own
# OAuth token (scope read:org is requested at /start), which verifies membership
# only while the OAuth App is org-approved for aws-e; when it isn't, the broker
# logs the fallback and redirects with error=org_check_unavailable rather than
# silently denying. To make org checks robust, create a Secrets Manager secret
# holding a GitHub token with read:org (suggested name
# adp/dev/gateway/github-org-token) and set its ARN here.
# github_auth_token_secret_arn = "arn:aws:secretsmanager:us-east-1:<account>:secret:adp/dev/gateway/github-org-token-XXXXXX"

# Issue #1013: Enable chat logging pipeline (cost-tracking EPIC).
# Provisions S3 chat-log bucket, usage_tracker + pricing_refresh Lambdas,
# EventBridge schedule, and S3→Lambda event notification.
enable_chat_logging = true

# Issue #1797 / #2213: grant the gateway IRSA role sqs:SendMessage on the
# agent-context ingestion queue so the Phase-1 inline dispatch (UI register /
# reindex of a knowledge asset → publish to SQS) works. Without this the
# register endpoint creates the DB row, the publish fails AccessDenied (caught
# and logged), and the asset is stuck at 'registered' — never indexed.
# Queue name is deterministic: <cluster>-context-ingestion (cluster =
# adp-dev-eks-cluster). Owned by modules/agent-context (sqs-ingestion module).
enable_agent_context_sqs          = true
agent_context_ingestion_queue_arn = "" # derive from the target account/region

# Issue #2709 (EPIC #2702): grant the gateway IRSA role bedrock:InvokeModel*
# so the mantle passthrough route (POST /openai/v1/responses) can SigV4-sign
# requests to bedrock-mantle with its own pod credentials. Pairs with
# BG_MANTLE_ENABLED in k8s/configmap.yaml — both must be on for the route
# to serve; flipping either off disables it.
enable_mantle_passthrough = true

# Issue #2910: Lambda reserved concurrency. The gateway reserves 97 total
# across 6 lambdas; unreserved must stay >= 100 (L-B99A9384). Disabled here
# because dev is where fresh/sandbox accounts get deployed — including
# SCP-locked / shared-pool accounts (e.g. #2899 on 979157915401) where the
# unreserved pool is pinned at the 100 floor and a quota increase is
# SCP-denied, so any reservation makes the apply's PutFunctionConcurrency
# fail. The var-file value overrides TF_VAR_ env, so this must be set here to
# be effective per-deploy. The variables.tf default stays true, so prod (and
# any env with headroom) keeps reserved-concurrency throttle isolation via
# its own tfvars / the default.
enable_lambda_reserved_concurrency = false

# Issue #3789: enable_webhook_secrets_kms_grant removed. The webhook-secrets
# CMK is now owned by platform infra and the gateway grant is unconditional.

# GitLab VPC Origin for CloudFront /gitlab/* behavior.
# GitLab is an OPTIONAL module — its values must NOT be hardcoded here.
# The gateway-infra-apply.yml workflow reads them from SSM at apply time and
# injects them as TF_VAR_gitlab_origin_* (platform account only); all other
# accounts fall back to the variables' empty defaults, which disables the
# GitLab origin. See #3745 / #3440 (core ≠ optional coupling rule).
# Do NOT re-add gitlab_origin_dns/arn assignments here, even empty ones: a
# -var-file entry takes precedence over TF_VAR_* env vars, so an empty pin
# silently overrides the workflow's SSM injection and makes every apply plan
# the DESTRUCTION of the live GitLab VPC origin.

# Issue #4313 / wave 4 (#4248): engine dispatch wiring for the orchestration tick.
# The tick resolves gate-approver genesis in-process and publishes the agent
# envelope itself (ruling: docs/design-notes/4303-engine-genesis-transport.md).
#
# Both values default to EMPTY in variables.tf, which is a deliberate fail-closed
# default: with either unset the tick dispatches nothing and reports
# `undispatchable`. That default is correct for a fresh/sandbox account, but it
# also means dev silently ran the dispatch code as a no-op until these were set.
#
# orchestration_dispatch_repo is REQUIRED because the orchestration graph does not
# carry a repository: OrchestrationNode stores only `issue_ref` (an issue number),
# while the agent worker hard-requires source_ref.{installation_id, repo, issue}.
#
# NOT hardcoded here: the queue URL/ARN. The queue is owned by the
# webhook-ingress Terraform state, so per the #3745 / #3440 core-vs-optional
# coupling rule the workflow reads it from
# /adp/<env>/webhook-ingress/sqs-queue-url at apply time and injects
# TF_VAR_orchestration_dispatch_queue_{url,arn}. Do NOT add those keys here even
# empty: a -var-file entry takes precedence over TF_VAR_*, so an empty pin would
# silently override the workflow's SSM injection and re-break dispatch.
orchestration_dispatch_repo = "aws-e/adp"

# Issue #4527: the orchestration engine (and its GitHub command bridge) is ON
# in dev — first live @agent-engine command proven 2026-08-31 on issue #4552.
# Without this pin every gateway-infra apply resets the tick Lambda's
# FEATURE_ORCHESTRATION_ENGINE_ENABLED back to the inert default (false) and
# silently disables the bridge until an operator re-sets the env var by hand.
orchestration_engine_enabled = true

# Prepare a Bedrock-only destination for bounded PMM qualification. Paid probe
# admission and its recurring schedule remain disabled separately.
persona_model_probe_destination_enabled = true

# Saved persona preferences resolve before dispatch; worker authority stays independent.
persona_model_mapping_enabled = true
