from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Database - fallback URL for local development (SQLite or password-based PostgreSQL)
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/bedrockgw"

    # RDS IAM Authentication settings
    # When rds_iam_auth=True, the app generates IAM auth tokens instead of using passwords
    rds_iam_auth: bool = False  # Set to True in production with RDS
    rds_host: str = ""  # RDS endpoint hostname (without port)
    rds_port: int = 5432  # RDS port
    rds_username: str = "bgadmin"  # Database username for IAM auth
    rds_dbname: str = "bedrockgateway"  # Database name
    agent_context_dbname: str = "agent_context"  # Knowledge Layer registry DB (Issue #2182)
    rds_tls_verify: bool = True  # Set BG_RDS_TLS_VERIFY=false only for emergency rollback

    # Redis (optional)
    redis_url: str | None = None

    # AWS
    aws_region: str = "us-east-1"

    # Auth
    api_key_duration_hours: int = 12
    helper_token_duration_minutes: int = 5
    token_secret_key: str = ""  # Required for JWT signing, must be set via BG_TOKEN_SECRET_KEY env var

    # Cognito OAuth Configuration
    cognito_user_pool_id: str = ""  # e.g., "us-east-1_5rYm3yrrY"
    cognito_client_id: str = ""  # Cognito app client ID
    cognito_domain: str = ""  # Cognito hosted UI domain prefix (e.g., "bedrockgw-dev-auth")

    # Server
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"

    # CloudWatch (optional)
    cloudwatch_log_group: str | None = None

    # API Gateway Auth Trust
    # When true, accepts X-Auth-Source and X-Agent-* headers from API Gateway
    # Only enable this for API Gateway routes (where Lambda authorizer sets headers)
    trust_apigw_headers: bool = False

    # Issue #260: DynamoDB Agent Registry Table
    # Used for IAM-authenticated agents via API Gateway /agent/* path
    # FastAPI reads IAM identity from API Gateway headers and looks up agent in DynamoDB
    agent_registry_table: str = ""

    # Issue #144: Tracing Configuration
    # Phase 1: Timing headers (always enabled by default)
    timing_header_enabled: bool = True

    # Phase 2: OpenTelemetry/X-Ray distributed tracing (opt-in)
    otel_enabled: bool = False
    otel_service_name: str = "bedrock-gateway"
    otel_exporter_endpoint: str = "http://localhost:4317"

    # Issue #446: Magic-link signing key.
    # If not set, falls back to token_secret_key (same key, different namespace via issuer check).
    # Set BG_MAGIC_LINK_SECRET to a separate high-entropy secret in production.
    magic_link_secret: str = ""

    # Issue #446: Internal API shared secret for Lambda → gateway calls.
    # Set BG_INTERNAL_API_KEY to a high-entropy secret in production.
    # The gateway validates X-Internal-Api-Key on all /internal/* endpoints.
    internal_api_key: str = ""

    # Issue #446: Base URL for magic-link landing page, e.g. "https://gateway.example.com"
    # Defaults to empty; tests inject directly.
    gateway_base_url: str = ""

    # Issue #137: Vault Phase 4 — enable credential MCP tools + adp-cred CLI.
    # When False, vault tools are not registered in the chat-agent and
    # adp-cred CLI returns 503 on every invocation.
    enable_user_credentials: bool = False

    # Issue #136: Vault Phase 3 — internal credential delivery paths.
    # When False (default), POST /internal/v1/credential-raw-read returns 403.
    # Enable per-org in production only after security review.
    vault_raw_read_enabled: bool = False

    # S3 bucket used by the credential-materialize path to stage short-lived
    # credential files for agent tmpfs writes.  Must be set in production.
    vault_materialization_bucket: str = ""

    # Issue #1158: Host allowlist for /internal/v1/proxy-request (SSRF mitigation).
    # Comma-separated list of allowed target hosts. Supports exact match and
    # wildcard prefix (e.g. "*.atlassian.net"). Empty = deny-all (fail-closed).
    vault_proxy_host_allowlist: str = ""
    # When True, only https:// URLs are accepted by the proxy-request endpoint.
    vault_proxy_require_https: bool = True

    # Issue #466: Well-known UUID for the adp-default free-tier tenant.
    # Every environment uses the same UUID so seed scripts and code agree.
    adp_default_org_id: str = "00000000-0000-4000-a000-000000000001"

    # Issue #465: GitHub App identity — the single App this deployment installs +
    # authenticates as for the "Link GitHub" / install flow and agent webhooks.
    # MUST be configured per deployment (set BG_GITHUB_APP_SLUG / BG_GITHUB_APP_ID
    # / BG_GITHUB_APP_PRIVATE_KEY via the gateway configmap + secret). There is no
    # hardcoded default on purpose: a wrong/empty value silently pointing the UI
    # at some other App is how the install flow breaks (the UI offers App X while
    # the gateway holds App Y's key → installs never attach). Empty = unconfigured;
    # the connections endpoints fail loudly rather than guess.
    github_app_slug: str = ""  # e.g. "adp-agent-platform" (the github.com/apps/<slug>)
    github_app_id: str = ""  # numeric GitHub App ID
    github_app_private_key: str = ""  # PEM-encoded RSA private key

    # Issue #2709: bedrock-mantle passthrough for OpenAI Responses-API traffic.
    # Lets Codex (and future OpenAI-model clients) route through the gateway so
    # OpenAI tokens get the same per-tenant metering + model-allowlist governance
    # that Claude traffic gets today. Route: POST /openai/v1/responses.
    mantle_enabled: bool = False  # Master switch; route returns 503 until enabled.
    # Base URL of the mantle endpoint WITHOUT the trailing path. The route appends
    # the GPT-5.5 quirk path itself ("/openai/v1/responses"). {region} is substituted
    # from mantle_region if the literal "{region}" appears in the value.
    mantle_base_url: str = "https://bedrock-mantle.{region}.api.aws"
    mantle_region: str = "us-east-1"
    # Upstream auth is SigV4 ONLY (operator decision 2026-07-03; spike #2703 §4-5
    # verified mantle accepts SigV4 with signing name "bedrock"). The gateway pod
    # signs with its ambient IRSA credential chain — no API keys, no Secrets
    # Manager entry. There is no selectable auth mode.
    # Comma-separated glob patterns of OpenAI model IDs the route will serve
    # (e.g. "openai.gpt-5.5,openai.*"). Used to validate the requested model
    # before proxying; per-tenant access is still enforced via the model allowlist.
    mantle_allowed_models: str = "openai.*"

    # Issue #3175: Credential-authorization binding (S2).
    # When True, credential endpoints ENFORCE registry-based user resolution:
    # missing invocation_id or empty authorized_user_id → 403.
    # When False (default), shadow mode: resolve from registry, compare to body,
    # emit drift/fallback metrics, but never block.
    enforce_credential_binding: bool = False
    # DynamoDB table name for webhook-events (used by credential binding to
    # resolve authorized_user_id). Set via SSM in prod.
    webhook_events_table: str = "adp-dev-webhook-events"

    # Issue #2918: Gate Base.metadata.create_all behind this flag.
    # Default False in deployed envs (migrations are the single source of truth).
    # Set True only for docker-compose / local dev where alembic isn't run on startup.
    db_auto_create: bool = False

    model_config = {"env_prefix": "BG_", "env_file": ".env"}


def get_settings() -> Settings:
    return Settings()
