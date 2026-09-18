"""Application settings loaded from environment variables."""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Control plane API server configuration.

    All values can be overridden via environment variables.
    For Aurora IAM auth in production, set DATABASE_URL to the Aurora endpoint
    and configure IAM authentication at the driver level.
    """

    # Application
    app_name: str = "superplane-api"
    app_version: str = "0.1.0"
    debug: bool = False

    # Database
    database_url: str = (
        "postgresql+asyncpg://superplane:superplane@localhost:5432/superplane"
    )

    # AWS
    aws_region: str = "us-east-1"
    aws_account_id: str = ""

    # Cognito
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""
    cognito_app_client_secret: str = ""

    # Domain auth enforcement (issue #5055, U14 — R5/R6).
    #
    # `domain_auth_enforced` is the switch, and it is OFF by default here on
    # purpose. Retiring the legacy self-signed JWT path is U21's conditional
    # story, so this story adds the strict path beside it rather than deleting
    # the old one. What the flag does NOT do is soften the strict path: when it
    # is on there is no fallback to the legacy validator, because a permissive
    # alternate validator that answers the same question is a bypass.
    #
    # There is deliberately no default issuer or client allowlist. An empty
    # allowlist is indistinguishable from having no policy at all, so
    # `build_domain_policy()` refuses to start with enforcement on and either
    # value unset (see app/auth.py) instead of quietly admitting every client
    # in the user pool.
    domain_auth_enforced: bool = False
    cognito_issuer: str = ""
    cognito_jwks_url: str = ""
    domain_auth_allowed_client_ids: list[str] = []

    # Recorded, never inferred (R5 acc. 5). Whether the *target environment*
    # fronts this API with Cognito is a per-environment fact; reading a code
    # default in this repository and calling it the environment's state is the
    # inference that criterion forbids. Asserted by the deployment, reported by
    # GET /health so a reader can observe it rather than assume it.
    cognito_enabled: bool = False

    # JWT Auth
    jwt_secret_key: str = "CHANGE-ME-IN-PRODUCTION"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # Internal API (machine-to-machine auth for bootstrap workflows)
    internal_api_token: str = ""

    # Observation contract v1 receiver (issue #5056, U15).
    #
    # A JSON array of submitter entries, each with `submitter_id`, `credential`,
    # `signing_key` and `workspaces`. Empty by default and therefore
    # fail-closed: with no entries configured, no credential resolves and every
    # submission is refused. An empty grant authorizing everything is how a
    # misconfigured deployment becomes a tenant-boundary failure, so the default
    # is "nobody" rather than "anybody".
    #
    # The value carries credentials, so it comes from the deployment's secret
    # store via the environment and is never logged. Nothing in this file holds a
    # real value.
    observation_submitters: str = ""

    # The legacy shared-token POST /internal/heartbeat. True preserves it so a
    # receiver can be deployed before any sender changes (see
    # docs/runbooks/superplane-monitor-grant-withdrawal.md); set false at the
    # cutover step, after which the route refuses with 410 and the authenticated
    # contract is the only write path.
    legacy_heartbeat_enabled: bool = True

    # Workspace provisioning runs through the authorized-operation facade
    # (app/services/provisioning.py), not GitHub Actions. The `github_token` /
    # `github_repo` settings were removed by issue #5058 (U17b): they held a
    # long-lived personal access token for a repository this project does not own,
    # and no runtime path reads them any more.

    # CORS
    cors_origins: list[str] = ["*"]

    # Rate limiting
    rate_limit_per_minute: int = 60

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()
