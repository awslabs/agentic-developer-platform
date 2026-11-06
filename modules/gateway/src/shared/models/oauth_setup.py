"""Deployment setup intents and leased recovery work; no user OAuth tokens."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, new_uuid


class OAuthSetupState(Base):
    __tablename__ = "oauth_setup_states"

    deployment_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    registration_mode: Mapped[str] = mapped_column(String(64), nullable=False)
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(String(255))
    client_ref: Mapped[str | None] = mapped_column(String(255))
    policy_result: Mapped[str | None] = mapped_column(String(32))
    policy_evidence_ref: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OAuthRecoveryJob(Base):
    __tablename__ = "oauth_recovery_jobs"
    __table_args__ = (
        UniqueConstraint("setup_key", "kind", "expected_revision", name="uq_oauth_recovery_intent"),
        Index("ix_oauth_recovery_due", "state", "due_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    setup_key: Mapped[str] = mapped_column(String(128), ForeignKey("oauth_setup_states.deployment_key"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_ref: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_category: Mapped[str | None] = mapped_column(String(64))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OAuthTenantPolicyObservation(Base):
    __tablename__ = "oauth_tenant_policy_observations"

    setup_key: Mapped[str] = mapped_column(String(128), ForeignKey("oauth_setup_states.deployment_key"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    config_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_result: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OAuthGrantRevocation(Base):
    """Retained, owner-bound grant cleanup intent; never stores Identity tokens."""

    __tablename__ = "oauth_grant_revocations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "owner_ref", "grant_revision", name="uq_oauth_grant_revocation_generation"),
        Index("ix_oauth_grant_revocation_due", "state", "due_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    grant_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    connection_id: Mapped[str | None] = mapped_column(String(36))
    provider_account_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_category: Mapped[str | None] = mapped_column(String(64))
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
