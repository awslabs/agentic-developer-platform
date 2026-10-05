"""Application settings loaded from environment variables."""

from typing import Literal

from pydantic import field_validator, model_validator
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
    # Deployment-owned tenant/target policy; never accepted from HTTP input.
    superplane_lifecycle_config_file: str = ""
    superplane_controller_profiles_file: str = ""
    superplane_operation_gateway_url: str = ""
    superplane_operation_gateway_region: str = ""
    # Omission preserves existing operation hosts; staged installs set false.
    superplane_operation_dispatch_enabled: bool = True
    # Deployment selection, never supplied by an admission request.
    superplane_paid_worker_mode: Literal[
        "legacy", "native-controller", "native-lifecycle"
    ] = "legacy"
    superplane_paid_worker_binding_file: str = ""

    @field_validator("superplane_operation_dispatch_enabled", mode="before")
    @classmethod
    def strict_dispatch_enabled(cls, value):
        if type(value) is bool:
            return value
        if value in ("true", "false"):
            return value == "true"
        raise ValueError("SUPERPLANE_OPERATION_DISPATCH_ENABLED must be true or false")

    controller_status_url: str = ""
    controller_registry_credential: str = ""

    # AWS
    aws_region: str = "us-east-1"
    aws_account_id: str = ""

    # Cognito
    cognito_user_pool_id: str = ""
    cognito_app_client_id: str = ""
    cognito_app_client_secret: str = ""

    # Production is strict by default. Legacy authentication is available only
    # through an explicit development profile and explicit enforcement opt-out.
    superplane_security_profile: Literal["production", "development"] = "production"
    domain_auth_enforced: bool = True
    # Additive current-identity integration, not a switch for existing JWT/grant
    # enforcement. Enable only with the protected ADP reader composed in API and
    # worker processes. Existing releases retain their signed-token/live-grant path.
    current_identity_enforced: bool = False

    @model_validator(mode="after")
    def require_production_auth(self):
        if (
            not self.domain_auth_enforced
            and self.superplane_security_profile != "development"
        ):
            raise ValueError(
                "DOMAIN_AUTH_ENFORCED=false requires SUPERPLANE_SECURITY_PROFILE=development"
            )
        return self

    cognito_issuer: str = ""
    cognito_jwks_url: str = ""
    domain_auth_allowed_client_ids: list[str] = []

    # Recorded, never inferred (R5 acc. 5). Whether the *target environment*
    # fronts this API with Cognito is a per-environment fact; reading a code
    # default in this repository and calling it the environment's state is the
    # inference that criterion forbids. Asserted by the deployment, reported by
    # GET /health so a reader can observe it rather than assume it.
    cognito_enabled: bool = False

    # Audit read coverage (issue #5673, A17).
    #
    # Mutating requests are ALWAYS audited and this flag does not affect them. It controls
    # only whether reads of tenant data are recorded too.
    #
    # OFF by default, deliberately. Reads are the bulk of traffic, so enabling this
    # multiplies audit row volume and puts a database write on the hot path of every GET;
    # that is a storage and latency decision each environment should make explicitly
    # rather than inherit from a code default. It also bounds an amplification risk: now
    # that refused attempts are recorded, a caller able to generate rejected reads can
    # drive audit writes, and a per-environment switch is what allows shedding that volume
    # without a code change.
    audit_read_coverage: bool = False

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

    # How long a credential-evidence read may take before it is abandoned
    # (issue #5535). Configurable because the acceptable bound is a property of the
    # deployment's network, not of this code: the value that is generous in one
    # cluster is an outage in another, and a constant in the adapter module can only
    # be changed by shipping a new image.
    #
    # It has a default, and the default is not "wait forever". An unbounded read
    # holds a request worker for as long as the vault stays silent, so a slow vault
    # becomes an exhausted pool and an API-wide outage — a much larger failure than
    # the one unavailable credential the caller asked about. 10s is long enough to
    # ride out a slow round trip and short enough that the boot-time capability
    # probe's own 5s bound (`app/capability_probes.py`) still governs at startup.
    #
    # Bounded above as well as below. Zero or a negative value would mean "time out
    # immediately", turning every read into a spurious "vault unavailable" and
    # reporting a healthy vault as broken; an unbounded upper end would reintroduce
    # the pool-exhaustion failure this setting exists to bound.
    adp_vault_timeout_seconds: float = 10.0

    @field_validator("adp_vault_timeout_seconds")
    @classmethod
    def _timeout_must_be_usable(cls, value: float) -> float:
        """Refuse at startup rather than on the first credential read.

        A misconfigured timeout that failed lazily would surface as an intermittent
        503 under load, which reads as a vault fault; refusing here names the setting
        that is actually wrong, while the deployment is still being rolled out.
        """
        if not 0 < value <= 120:
            raise ValueError(
                "adp_vault_timeout_seconds must be greater than 0 and at most 120"
            )
        return value

    # Cross-origin browser access requires an explicit allowlist (#5682).
    # Empty supports same-origin gateway deployments; the installer supplies its
    # reviewed origin. CORS does not authenticate a caller or supply a bearer
    # token. Wildcard credentialed preflights can reflect arbitrary origins, so
    # resolve_cors_origins() rejects that configuration at startup.
    cors_origins: list[str] = []

    # Rate limiting
    rate_limit_per_minute: int = 60

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()


class WildcardCORSWithCredentials(RuntimeError):
    """A wildcard origin was configured on a credentialed API."""


def resolve_cors_origins() -> list[str]:
    """Reject unsupported wildcard CORS configuration before serving requests.

    A configuration error is explicit rather than silently replacing an operator's
    configured allowlist and leaving browser clients with unexplained failures.
    """
    origins = [origin.strip() for origin in settings.cors_origins if origin.strip()]
    if "*" in origins:
        raise WildcardCORSWithCredentials(
            "CORS_ORIGINS contains '*', which is not allowed with credentialed "
            "cross-origin requests. List each reviewed origin explicitly, or "
            "leave CORS_ORIGINS unset if the frontend is served same-origin."
        )
    return origins


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
