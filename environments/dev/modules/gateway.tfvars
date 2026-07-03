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
