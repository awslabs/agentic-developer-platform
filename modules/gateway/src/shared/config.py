from typing import Any

from pydantic import model_validator
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

    # Redis / ElastiCache IAM authentication (Issue #4342)
    # ElastiCache has the `default` user disabled and an IAM-auth user provisioned,
    # so connections must present a SigV4 token as the password. Mirrors the
    # rds_iam_auth switch above; false keeps local dev / docker-compose passwordless.
    redis_iam_auth: bool = False
    redis_username: str = ""  # Provisioned ElastiCache IAM-auth user name
    redis_cache_name: str = ""  # Replication group id — what the token is signed against

    # AWS
    aws_region: str = "us-east-1"

    # Auth
    api_key_duration_hours: int = 12
    helper_token_duration_minutes: int = 5
    token_secret_key: str = ""  # Required for JWT signing, must be set via BG_TOKEN_SECRET_KEY env var

    # Cognito OAuth Configuration
    cognito_user_pool_id: str = ""  # e.g., "us-east-1_5rYm3yrrY"
    cognito_client_id: str = ""  # Cognito app client ID
    # CLI-specific app client (web CLI login). Short refresh validity +
    # rotation, minted by src/auth/cli_login.py. Empty = feature disabled
    # (endpoints 503) — safe before the Terraform that creates it applies.
    cognito_cli_client_id: str = ""
    # Either a hosted-UI domain PREFIX ("bedrockgw-dev-auth") or a custom-domain
    # FQDN ("auth.example.com"). Consumers distinguish them on the presence of a
    # dot, since a prefix is a single DNS label — see agent_service.py, which
    # builds the agent M2M token endpoint from this value. Setting a prefix once a
    # custom domain has replaced it yields a host that no longer exists, and the
    # failure surfaces only when an agent attempts a token exchange.
    cognito_domain: str = ""

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

    # Issue #4076: Credential->host egress binding. When True, a credential whose
    # service appears in SERVICE_HOST_BINDINGS (see internal/credential_egress.py)
    # may only be injected into requests to that service's own hosts; anything
    # else is 403 + an audit row. When False (default), shadow mode: violations
    # are logged (WARN) but allowed, so the map's coverage gaps surface in logs
    # before anyone gets a 403 — and rollback is a config flip, not a redeploy.
    # Services with no map entry are never bound (the service column is
    # deliberately free-form); the host allowlist above remains their control.
    vault_enforce_credential_host_binding: bool = False

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
    #
    # This is AWS Bedrock's OpenAI-compatible endpoint, which lives on the same
    # host family as the native Bedrock runtime (bedrock-runtime.<region>.amazonaws.com).
    # It replaces the earlier preview host bedrock-mantle.<region>.api.aws, which
    # only served a curated model subset (e.g. gpt-5.6-sol) and never picked up
    # newer models like gpt-6-astra (returned 404 "model does not exist"). Unlike
    # that preview host, bedrock-runtime serves OpenAI models ONLY via inference
    # profiles, so bare on-demand ids are rejected — see
    # mantle_inference_profile_prefix below, which restores the bare-id UX.
    mantle_base_url: str = "https://bedrock-runtime.{region}.amazonaws.com"
    mantle_region: str = "us-east-1"
    # Geo prefix for the cross-region inference profile the mantle route forwards
    # under. bedrock-runtime rejects bare foundation-model ids for the flagship
    # OpenAI families on this path ("Invocation ... with on-demand throughput isn't
    # supported. Retry ... with an inference profile") — gpt-5.6-*/gpt-6-* are
    # invocable ONLY via their inference profile (us.openai.*, global.openai.*, ...).
    # To keep the caller's id stable (Codex/config keep using bare openai.gpt-6-astra)
    # the route rewrites the FORWARDED body's model to "<prefix>.<model>" before
    # signing, while metering/pricing stay keyed on the bare id. Set to "" to
    # disable the rewrite (e.g. to point back at a host that maps bare ids itself).
    # Values: the Bedrock geo prefix for mantle_region — "us", "eu", "apac", or
    # "global". On-demand models are exempted via mantle_on_demand_models below.
    mantle_inference_profile_prefix: str = "us"
    # Comma-separated globs of OpenAI models invoked ON-DEMAND with the bare id (no
    # inference profile exists for them, so the geo-prefix rewrite above must skip
    # them — prefixing would yield an invalid id). The gpt-oss family is on-demand;
    # the gpt-5.6/gpt-6 flagship families are inference-profile-only and SHOULD be
    # prefixed, so they are deliberately NOT listed here. New flagship models thus
    # get the profile prefix automatically; only add a pattern here if a future
    # model is genuinely on-demand on the Responses API.
    mantle_on_demand_models: str = "openai.gpt-oss*"
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
    enforce_credential_binding: bool = True
    # DynamoDB table name for webhook-events (used by credential binding to
    # resolve authorized_user_id). Set via SSM in prod.
    webhook_events_table: str = "adp-dev-webhook-events"

    # Issue #3989: IAM role-name prefixes reserved for platform-owned roles.
    # An agent-registry row resolves ANY registered role_arn to an authenticated
    # `service` identity in that row's org (src/auth/agent_registry.py), and
    # role_arn is unique. Registering a platform-owned role (e.g.
    # "adp-dev-agent-runner-role") would therefore bind the platform's own CI
    # role into an attacker-chosen org AND squat the ARN so the legitimate row can
    # never be created. Role names starting with any of these prefixes are
    # rejected on every registry write regardless of caller privilege.
    # Comma-separated; matched case-insensitively against the role NAME (the ARN
    # segment after "role/", path stripped).
    reserved_role_name_prefixes: str = "adp-,bedrockgw-"

    # Issue #2918: Gate Base.metadata.create_all behind this flag.
    # Default False in deployed envs (migrations are the single source of truth).
    # Set True only for docker-compose / local dev where alembic isn't run on startup.
    db_auto_create: bool = False

    # Issue #4144: gate inference on approval (org assignment). When True, a human
    # caller with no org assignment (resolved from Postgres, not just the JWT
    # claim) is rejected with a 409 on the enforced spend paths. Platform admins
    # and agents/service accounts are exempt. Default False — enable per-env after
    # smoke. Set BG_ENFORCE_ORG_ASSIGNMENT=false to roll back (checked per-request,
    # so a pod recycle is enough; no image rebuild).
    enforce_org_assignment: bool = False

    # Issue #4743 (#4692 · R2): per-principal Bedrock account routing, SHADOW MODE.
    # When True the resolution ladder (user > team > org > platform) runs on the
    # settlement path and its answer is recorded in usage_logs.bedrock_account_id.
    # It does NOT change where any request goes — the call is already signed and
    # sent by the time this runs, with the platform account's ambient IRSA
    # credentials, exactly as on main. Enforcement is #4744 (R3).
    #
    # Default True: with zero mappings configured the existence gate makes this
    # cost zero queries per model call, and the captured column is what #4744 is
    # gated on — an operator has to be able to see what routing *would* do before
    # being asked to let it happen. Set BG_BEDROCK_ROUTING_SHADOW_MODE=false to
    # stop observing (read per-request, so a pod recycle is enough; no rebuild).
    bedrock_routing_shadow_mode: bool = True

    # Issue #4743: the account the gateway's own IRSA credentials belong to — the
    # answer for the platform rung, i.e. every call today. Configured rather than
    # discovered because a live sts:get_caller_identity on the settlement path
    # would add a network hop to every request that resolves to platform (which,
    # before any mapping exists, is all of them).
    #
    # Empty means "not captured": the platform rung still resolves, but the column
    # is left NULL rather than filled with a guess. NULL is a truthful "we did not
    # capture this"; a fabricated account id reads as evidence and would mislead
    # exactly the audit this column exists to support.
    platform_bedrock_account_id: str = ""

    # Issue #4744: how long an assumed destination session is requested for, and
    # how early to refresh it. Same values and same reasoning as the pool's
    # PoolSettings (src/pool/config.py:42-46), which the design note (§2.3) says to
    # reuse verbatim: refresh BEFORE expiry rather than expiring into a failed
    # call, because under fail-closed an expired credential is an outage rather
    # than a retry.
    bedrock_routing_session_duration_seconds: int = 3600
    bedrock_routing_credential_refresh_margin_seconds: int = 300

    # Issue #4744: LRU bound on the destination credential cache. The key is the
    # full identity tuple (§2.3), so the key space grows with the number of
    # DISTINCT (org, role, region, rung) destinations — not with the number of
    # users, since principals mapped to the same destination share an entry
    # legitimately. The dead pool cache it replaces was an unbounded dict, which
    # was fine for a static 2-account pool and is a memory-growth problem the
    # moment the key space is principal-dependent.
    bedrock_routing_credential_cache_size: int = 256

    @model_validator(mode="before")
    @classmethod
    def ignore_retired_routing_switch(cls, values: Any) -> Any:
        """Accept legacy .env files without restoring a routing bypass.

        Dotenv includes unknown keys even after their setting is removed. Ignore
        this retired key only; keep validation of other unknown settings strict.
        """
        if isinstance(values, dict):
            retired = {"bedrock_routing_enforce", "bg_bedrock_routing_enforce"}
            return {key: value for key, value in values.items() if key not in retired}
        return values

    model_config = {"env_prefix": "BG_", "env_file": ".env"}


def get_settings() -> Settings:
    return Settings()
