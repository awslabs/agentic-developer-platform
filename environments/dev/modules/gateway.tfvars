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

# Sign-in requires an existing platform organization membership. This uses the
# server-maintained membership projection, avoiding the OAuth org-token fallback.
# Missing/unavailable membership data denies sign-in; it never enables signup.
github_auth_allowlist_mode    = "platform"
github_auth_allow_open_signup = false
github_auth_allowed_orgs      = "aws-e"

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

# Operator-activated Task API bindings; acceptance evidence is recorded separately.
task_api_prerequisites_enabled = true
task_api_artifact_bucket_name  = "adp-dev-chat-artifacts-879318057152"
task_api_flags                 = { admission = true, read = true, worker = true, recovery = true }
task_api_runtime_bindings = {
  queue_url                = "https://sqs.us-east-1.amazonaws.com/879318057152/adp-dev-agent-submit.fifo"
  admission_producer_roles = ["arn:aws:iam::879318057152:role/adp-dev-webhook-lambda-role"]
  dispatch_producer_roles  = ["arn:aws:iam::879318057152:role/adp-dev-webhook-lambda-role"]
  recovery_producer_roles  = ["arn:aws:iam::879318057152:role/adp-dev-webhook-lambda-role"]
  qualification_id         = "task-api-5792-20260925"
  worker_image_digests     = ["sha256:1cb3550ee64d72b3b5261ccba7c874378a309d71ca277cb985dc4c911edce51f", "sha256:2923326ff83e0335cbf9e17a0c0f80b6c2ab0b01c70c76fac9fd059851b6eb94", "sha256:3d1275bea78f6b400ddc7402822abf4785c2be284b7ffcc9d6d47e7710522010", "sha256:3d19d3fba77538bba11d57c9ef05026e54b70bf465515bca42746f3534cf96e4", "sha256:5d3e952c1be21b1a2656b783fbebf0d653893469d9bc17e7f0ca623cef3f8572", "sha256:68569bf10df1e76603d3ece24338a940920865720597986acd77474d8de28d47", "sha256:6beab1ad04b9249aa72b2f25bf6b8bbfc3f8ecfc82eac250ac2eb8e083819d67", "sha256:7c1541d515717d54da677797ebd37cac67b39f3e8de116cd6e391f0de08b201d", "sha256:8a3af964d0a786a6c27b5e66d44ac32e8e451c23d375046663936d2007658db1", "sha256:a396eeae0ebd032866b33550a876aeae7a317d0bce00163608d790b4bafc55c3", "sha256:beae9a4b9e6c6ebd4f442eebf2a65965d077b072a76baa0563caa689289979d4", "sha256:cdde81fb9cf747676942136c2e68de51bf7e3bb6dd9bbeefee613abcaac69d1e", "sha256:cf0678b1bef6f1562eab17a06a12568081350b6eac7f187bd1df6b2166ee95cf", "sha256:e25a54c3037f9bde26a3aa400e3af9bf367288e7a6977cece2d508f5f259fe27", "sha256:f14998af44cc77df24365371675ffe7fef54878d7a254c83e65a9dcbcbfdbabe", "sha256:f90e802c40b20edffd7ae7ccaa7ba6af1072158e7ae349535d6a1728e2bfa63d"]
  worker_service_account   = "agent-scaledjob-sa"
}

# Existing public task route targets the shared webhook Lambda; route presence is
# independent of admission.
enable_task_api_route         = true
task_api_lambda_function_name = "adp-dev-github-webhook"
task_api_lambda_invoke_arn    = "arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/arn:aws:lambda:us-east-1:879318057152:function:adp-dev-github-webhook/invocations"

# Match the protected worker and gateway dispatch protocol.
orchestration_agent_authority_enabled = true
