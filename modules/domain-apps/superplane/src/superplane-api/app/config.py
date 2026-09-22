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
    database_url: str = ""
    # Explicit domain schema for asyncpg (PGOPTIONS is a libpq setting, ignored
    # by this driver). Empty preserves the existing database-owned search_path.
    superplane_db_schema: str = ""
    # Trusted identity that may advance controller liveness; no reporter-name trust.
    controller_observation_submitter_id: str = ""

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
    #
    # There is deliberately NO default signing key (issue #5683, A04). The value
    # shipped here until now was a committed placeholder, which is a credential in
    # the repository: this key both signs and verifies the org-scoped tokens that
    # `/auth/login` issues, so anyone able to read this file could mint a token the
    # server accepts as an authenticated organization. It was accepted at runtime
    # rather than rejected, so a deployment that simply never set JWT_SECRET_KEY
    # ran on the published value without any signal that it had.
    #
    # WHY THE DEFAULT IS EMPTY RATHER THAN "A SAFER KEY", AND WHY EMPTY IS NOT
    # ITSELF THE FIX. `jose.jwt.encode` signs happily with an empty string — it
    # raises only on a non-string key. So an empty default is not "no key", it is a
    # key every reader can guess, which is the same defect with a shorter value.
    # Empty here means "unset", and the refusal is enforced by
    # `require_jwt_secret_key()` in app/middleware/auth.py, which every sign and
    # verify path goes through, plus a startup check in app/main.py so a
    # misconfigured deployment fails to start instead of failing at first login.
    #
    # Deployments supply the real value by reference from their secret store; see
    # `installation/manifests.py`, which injects JWT_SECRET_KEY as a secretKeyRef
    # alongside the other runtime secrets. Nothing in this file holds a real value.
    jwt_secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60

    # Internal API (machine-to-machine auth for bootstrap workflows)
    internal_api_token: str = ""

    # Observation contract v1 receiver (issue #5056, U15).
    #
    # A JSON array of submitter entries, each with `submitter_id`, `credential`,
    # `signing_key` and `workspaces` (immutable workspace UUIDs, never names).
    # Optional `lease_scopes: ["budget_monitor/global"]` authorizes the existing
    # global budget lease explicitly; normal cluster leases use workspace grants.
    # Empty by default and therefore
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

    # ADP vault access for credential evidence and operation-bound delivery
    # (issue #5528, w6-05).
    #
    # Both empty by default, and that default is fail-closed rather than
    # permissive: with no URL or key configured, `build_vault_client()` returns
    # None, no evidence reader is installed, and the provider-connection routes
    # answer 503 "ADP vault evidence is unavailable". That is the honest answer
    # for a deployment that was never given vault credentials — as opposed to a
    # 403, which would report a configuration gap as an authorization decision
    # and send an operator to check permissions that are fine.
    #
    # There is deliberately no default URL. A default pointing at some in-cluster
    # hostname would make a misconfigured deployment silently talk to whatever
    # answers there, and the thing on the other end of this connection is asked
    # to hand over credential values.
    #
    # `adp_gateway_internal_api_key` is the Gateway's internal shared secret. It
    # comes from the deployment's secret store via the environment, is sent only
    # as a request header (never in a URL, which reaches access logs where
    # headers do not), and is never logged. Nothing in this file holds a real
    # value.
    adp_gateway_internal_url: str = ""
    adp_gateway_internal_api_key: str = ""

    # CORS
    cors_origins: list[str] = ["*"]

    # Rate limiting
    rate_limit_per_minute: int = 60

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()


class DatabaseURLMissing(RuntimeError):
    """No managed database URL was supplied to this process."""


def require_database_url() -> str:
    """Return the configured database URL, or fail without exposing its contents."""
    url = settings.database_url
    if not url or not url.strip():
        raise DatabaseURLMissing(
            "DATABASE_URL is not set. Supply it by reference from the deployment's "
            "managed secret; no built-in database credential is available."
        )
    return url
