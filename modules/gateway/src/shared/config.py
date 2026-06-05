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

    model_config = {"env_prefix": "BG_", "env_file": ".env"}


def get_settings() -> Settings:
    return Settings()
