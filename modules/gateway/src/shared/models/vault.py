"""Vault models: user identities, credentials, and channel-tenant mapping.

Issue #134: Vault Phase 1 — schema + secret-store substrate
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, TenantMixin, new_uuid, utcnow

# ---------------------------------------------------------------------------
# Enums (stored as varchar in DB for portability with SQLite tests)
# ---------------------------------------------------------------------------


class IdentityProvider(StrEnum):
    slack = "slack"
    github = "github"
    whatsapp = "whatsapp"
    discord = "discord"


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


class UserCredential(Base, TenantMixin):
    """Per-user credential metadata. Secret values live in AWS Secrets Manager."""

    __tablename__ = "user_credentials"
    __table_args__ = (
        Index("uq_user_credentials_user_service_label", "user_id", "service", "label", unique=True),
        Index("ix_user_credentials_org_id_service", "org_id", "service"),
        Index("ix_user_credentials_org_id_team_id_service", "org_id", "team_id", "service"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # team_id is denormalised from users.team_id at insert time. See UserIdentity
    # for the rationale.
    team_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    service: Mapped[str] = mapped_column(String(255), nullable=False)
    credential_type: Mapped[str] = mapped_column(String(20), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    secret_arn: Mapped[str] = mapped_column(String(512), nullable=False)
    scopes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)

    # Relationship
    user = relationship("User", backref="credentials", lazy="selectin", passive_deletes=True)


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
