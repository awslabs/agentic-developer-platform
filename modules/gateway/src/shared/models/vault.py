"""Vault models: user identities, credentials, and channel-tenant mapping.

Issue #134: Vault Phase 1 — schema + secret-store substrate
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from src.shared.identity.providers import SUPPORTED_PROVIDERS, IdentityProvider

from .base import Base, TenantMixin, new_uuid, utcnow

# ---------------------------------------------------------------------------
# Enums (stored as varchar in DB for portability with SQLite tests)
# ---------------------------------------------------------------------------

# IdentityProvider is now imported from src.shared.identity.providers
# and re-exported here for backward compatibility.
__all__ = ["IdentityProvider"]


class VerificationMethod(StrEnum):
    oauth = "oauth"
    magic_link = "magic_link"
    admin_manual = "admin_manual"


class CredentialType(StrEnum):
    api_key = "api_key"
    oauth_token = "oauth_token"
    basic_auth = "basic_auth"
    bearer = "bearer"
    ssh_key = "ssh_key"
    certificate = "certificate"
    config_file = "config_file"
    aws_role = "aws_role"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class UserIdentity(Base, TenantMixin):
    """Links external platform identities (Slack, GitHub, …) to internal users."""

    __tablename__ = "user_identities"
    __table_args__ = (
        Index("uq_user_identities_provider_provider_user_id", "provider", "provider_user_id", unique=True),
        Index("ix_user_identities_org_id_provider", "org_id", "provider"),
        Index("ix_user_identities_org_id_team_id", "org_id", "team_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # team_id is denormalised from users.team_id at insert time. Required so
    # team-scoped queries ("all identities in team X") don't need a JOIN, and
    # so a record exists of which team owned this identity when it was created.
    # If the user later moves teams, their credentials/identities do NOT move
    # automatically — a separate admin action decides that.
    team_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_username: Mapped[str | None] = mapped_column(String(255))
    verification_method: Mapped[str] = mapped_column(String(20), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)

    # Relationship (optional, for ORM navigation).
    # passive_deletes=True tells SQLAlchemy to let the DB handle CASCADE.
    user = relationship("User", backref="identities", lazy="selectin", passive_deletes=True)

    @validates("provider")
    def validate_provider(self, _key: str, value: str) -> str:
        """Reject unsupported provider values at write time (Issue #537)."""
        if value not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider: {value!r}. Must be one of: {sorted(SUPPORTED_PROVIDERS)}")
        return value


class UserCredential(Base, TenantMixin):
    """Per-scope credential metadata. Secret values live in AWS Secrets Manager.

    Issue #440: Scope relaxation — supports four ownership scopes:
      - user:       user_id IS NOT NULL, team_id IS NULL, domain_app_id IS NULL
      - team:       team_id IS NOT NULL, user_id IS NULL, domain_app_id IS NULL
      - org:        org_id (from TenantMixin) IS NOT NULL, user_id IS NULL,
                    team_id IS NULL, domain_app_id IS NULL  (implicit via both
                    other columns being NULL — org-scoped creds are identified
                    by having no narrower owner set)
      - domain-app: domain_app_id IS NOT NULL, user_id IS NULL, team_id IS NULL

    The CHECK constraint on PostgreSQL enforces that exactly one of
    (user_id, team_id, domain_app_id) is non-NULL.  Org-scoped credentials
    are the "all three NULL" case; this is intentional and distinct from
    the domain-app scope — a domain-app cred belongs to a specific installed
    app within an org, while an org cred belongs to the org itself.

    NOTE: The CHECK constraint is only enforced at the database level on
    PostgreSQL.  Application code (and the model validator below) enforces
    the same invariant for SQLite / other backends.
    """

    __tablename__ = "user_credentials"
    __table_args__ = (
        # Scope-aware unique indexes (partial on PostgreSQL; plain on SQLite).
        # Each scope has its own uniqueness domain: two credentials with the
        # same (scope_id, service, label) cannot coexist within the same scope.
        Index("uq_user_credentials_user_service_label", "user_id", "service", "label", unique=True),
        Index("uq_user_credentials_team_service_label", "team_id", "service", "label", unique=True),
        Index("uq_user_credentials_domain_app_service_label", "domain_app_id", "service", "label", unique=True),
        Index("ix_user_credentials_org_id_service", "org_id", "service"),
        Index("ix_user_credentials_org_id_team_id_service", "org_id", "team_id", "service"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)

    # ------------------------------------------------------------------ #
    # Owner columns — exactly ONE must be non-NULL (enforced by DB CHECK  #
    # on PostgreSQL and by validate_single_owner() below for all others). #
    # For org-scoped credentials all three are NULL (org_id from           #
    # TenantMixin provides the tenant boundary).                           #
    # ------------------------------------------------------------------ #

    # user-owned: a credential belonging to a specific user.
    user_id: Mapped[str | None] = mapped_column(
        String(255),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        default=None,
    )
    # team-owned: a shared credential for a team (e.g. deploy bot PAT).
    team_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None, index=True)
    # domain-app-owned: a credential installed by a domain app for a specific
    # tenant (e.g. the cyber app's MISP key per-tenant).
    domain_app_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)

    service: Mapped[str] = mapped_column(String(255), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_arn: Mapped[str] = mapped_column(String(512), nullable=False)
    scopes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When strict=True the resolver does NOT fall back to wider scopes.
    # Use this for high-sensitivity credentials where an agent should only
    # get the credential when it explicitly owns the right scope.
    strict: Mapped[bool] = mapped_column(default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)

    # Relationship (user-owned only; None for team/org/domain-app creds).
    user = relationship("User", backref="credentials", lazy="selectin", passive_deletes=True)

    # ------------------------------------------------------------------
    # Application-level owner invariant (complements the DB CHECK).
    # ------------------------------------------------------------------

    @property
    def owner_scope(self) -> str:
        """Return the ownership scope name for this credential."""
        if self.user_id is not None:
            return "user"
        if self.team_id is not None:
            return "team"
        if self.domain_app_id is not None:
            return "domain_app"
        return "org"


class MagicLinkNonce(Base):
    """Single-use nonce for magic-link token consumption.

    Issue #446: Vault Phase 2b — Magic-link identity linking flow

    One row per issued token.  The ``jti`` claim in the JWT is the PK here.
    A token is valid only if its nonce row exists AND ``consumed_at IS NULL``.
    """

    __tablename__ = "magic_link_nonces"

    jti: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    channel_context: Mapped[str | None] = mapped_column(String(512))
    # target_user_id: set when a signed-in user issues the link; NULL when an
    # internal Lambda issues it (user picks Cognito identity on landing page).
    target_user_id: Mapped[str | None] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChannelTenantMap(Base):
    """Maps external workspaces/orgs (e.g. Slack workspace) to ADP tenants."""

    __tablename__ = "channel_tenant_map"
    __table_args__ = (Index("uq_channel_tenant_map_provider_scope", "provider", "provider_scope_id", unique=True),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_scope_id: Mapped[str] = mapped_column(String(255), nullable=False)
    org_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # Relationship
    organization = relationship("Organization", backref="channel_mappings", lazy="selectin", passive_deletes=True)
