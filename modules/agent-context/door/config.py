"""Configuration for the Context MCP Server.

Reads from environment variables (injected via K8s ConfigMap envFrom).
Follows the same pattern as images/ingestion/config.py.
"""

from __future__ import annotations

import os


class ServerConfig:
    """Context MCP Server configuration from environment variables."""

    def __init__(self) -> None:
        # Zoekt (exact code search)
        self.zoekt_url: str = os.environ.get(
            "ZOEKT_URL", "http://zoekt.agent-context.svc.cluster.local:6070"
        )
        self.zoekt_timeout: float = float(os.environ.get("ZOEKT_TIMEOUT", "10.0"))

        # S3 (content store — code-indexes, wikis, repos)
        self.s3_bucket: str = os.environ.get("S3_BUCKET_NAME", "")
        self.s3_content_prefix: str = os.environ.get("S3_CONTENT_PREFIX", "content")
        self.s3_region: str = os.environ.get("AWS_REGION", "us-east-1")
        self.code_index_s3_prefix: str = os.environ.get(
            "CODE_INDEX_S3_PREFIX", "content/code-indexes"
        )

        # S3 Vectors (semantic search — optional)
        self.s3_vectors_bucket: str = os.environ.get("S3_VECTORS_BUCKET_NAME", "")
        self.s3_vectors_region: str = os.environ.get("S3_VECTORS_REGION", "")
        self.semantic_enabled: bool = os.environ.get(
            "SEMANTIC_SEARCH_ENABLED", "false"
        ).lower() in ("true", "1", "yes")

        # LiteLLM proxy (for embeddings in semantic/experience)
        self.litellm_url: str = os.environ.get(
            "LLM_BASE_URL", "http://litellm-proxy.agent-context.svc.cluster.local:4000/v1"
        )

        # Postgres (catalog for browse + ACL store)
        # Static DSN (local/CI fallback only — production uses IAM auth)
        self.database_url: str = os.environ.get("DATABASE_URL", "")
        # IAM auth (production): connect via RDS IAM tokens instead of password
        self.db_use_iam_auth: bool = os.environ.get("DB_USE_IAM_AUTH", "false").lower() in (
            "true",
            "1",
            "yes",
        )
        self.db_host: str = os.environ.get("DB_HOST", "")
        self.db_port: int = int(os.environ.get("DB_PORT", "5432"))
        self.db_name: str = os.environ.get("DB_NAME", "agent_context")
        self.db_user: str = os.environ.get("DB_USER", "agent_context_rw")

        # Neptune (graph database for structural queries)
        self.neptune_endpoint: str = os.environ.get("NEPTUNE_ENDPOINT", "")
        self.neptune_enabled: bool = os.environ.get("NEPTUNE_ENABLED", "false").lower() in (
            "true",
            "1",
            "yes",
        )

        # Tenant scoping defaults on; only an explicit development profile may disable it.
        self.tenant_scope_enabled: bool = os.environ.get(
            "TENANT_SCOPE_ENABLED", "true"
        ).strip().lower() not in ("false", "0", "no")

        # Project scoping (E9 — kill switch)
        self.project_filter_enabled: bool = os.environ.get(
            "PROJECT_FILTER_ENABLED", "false"
        ).lower() in ("true", "1", "yes")

        # Server
        self.host: str = os.environ.get("MCP_HOST", "0.0.0.0")
        self.port: int = int(os.environ.get("MCP_PORT", "5100"))

        # Door authentication (issue #4073, finding #8).
        #
        # The Door reads caller identity (x-github-login, x-github-teams,
        # x-tenant-id, x-owner-sub) straight off request headers and uses it to
        # scope every ACL decision. Until #4073 nothing authenticated the caller,
        # so any pod that could reach the ClusterIP could set those headers
        # itself and read any tenant's indexed code and agent memory. The
        # docstrings in acl.py / personal_context/identity.py claimed an
        # in-cluster NetworkPolicy prevented that; no such policy existed.
        #
        # The shared secret is the same value the gateway uses for its
        # /internal/v1/* plane (Secrets Manager adp/<env>/gateway/internal-api-key,
        # bridged into this namespace by agent-context-deploy.yml), so callers
        # that already hold it need no new credential.
        self.door_api_key: str = os.environ.get("DOOR_API_KEY", "")
        # Kill switch, defaulting to ENABLED.
        #
        # NOTE the inverted parse: this deliberately does NOT follow the
        # `in ("true", "1", "yes")` idiom used by the enable-flags above. Those
        # default to "false", so an unrecognized value failing to "off" is safe.
        # This flag defaults to "true", so the same idiom would make any
        # unrecognized value — a typo like "ture", a YAML-quoted "True "
        # with trailing space, an empty string from an unset ConfigMap key —
        # evaluate to False and silently DISABLE authentication, reopening the
        # #4073 cross-tenant read with no signal. Authentication is therefore
        # switched off only by an explicit, recognized false-y value.
        self.door_auth_enabled: bool = os.environ.get(
            "DOOR_AUTH_ENABLED", "true"
        ).strip().lower() not in ("false", "0", "no")

        self.security_profile = (
            os.environ.get("DOOR_SECURITY_PROFILE", "production").strip().lower()
        )
        if self.security_profile not in ("production", "development"):
            raise ValueError("DOOR_SECURITY_PROFILE must be production or development")
        if self.security_profile != "development" and not (
            self.tenant_scope_enabled and self.door_auth_enabled
        ):
            raise ValueError(
                "Disabling Door authentication or tenant scoping requires DOOR_SECURITY_PROFILE=development"
            )

    @property
    def db_configured(self) -> bool:
        """Whether a usable ACL-store connection is configured.

        Mirrors the branch order in ``db.create_db_pool`` exactly: IAM auth
        needs both the flag and a host, otherwise a static DSN is required.
        """
        return bool(self.db_use_iam_auth and self.db_host) or bool(self.database_url)

    def missing_required(self) -> list[str]:
        """Names of absent settings the Door cannot safely serve reads without.

        Only genuinely load-bearing settings belong here. The ACL store is the
        whole cross-tenant boundary, so an unconfigured database is a hard
        startup failure rather than a degraded mode (#5658). Optional
        enrichments — Neptune, S3 Vectors, semantic search — are deliberately
        excluded: their absence loses features, not containment.

        ``DOOR_API_KEY`` is likewise excluded on purpose. ``door/auth.py``
        already rejects every authenticated path with 503 "not_configured" when
        it is unset, so a missing key denies reads instead of allowing them.

        ``S3_BUCKET_NAME`` is excluded for the same reason: with no bucket the
        object-store backends return nothing, which is a loss of function and
        not a loss of isolation. Gating readiness on it would strand otherwise
        safe configurations without improving containment.
        """
        missing: list[str] = []
        if not self.db_configured:
            missing.append("DB_USE_IAM_AUTH+DB_HOST or DATABASE_URL")
        return missing


# Singleton — import this in server modules
config = ServerConfig()
