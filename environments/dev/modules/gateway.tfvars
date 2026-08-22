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
github_auth_allowlist_mode = "org"
github_auth_allowed_orgs   = "aws-e"

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
agent_context_ingestion_queue_arn = "arn:aws:sqs:us-east-1:879318057152:adp-dev-eks-cluster-context-ingestion"

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
# The gateway-infra-apply.yml workflow reads them from SSM at apply time
# (platform account only); all other accounts get the empty default which
# disables the GitLab origin. See #3745 / #3440 (core ≠ optional coupling rule).
gitlab_origin_dns = ""
gitlab_origin_arn = ""
