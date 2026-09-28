"""Onboarding models: tenant access requests, tenants, and memberships.

Issue #538: Onboarding flow — GitHub sign-in to tenant + user creation.
Issue #2961: D5 data foundation — tenant_memberships table.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, new_uuid, utcnow


class TenantAccessRequest(Base):
    """Tracks onboarding access requests from new users."""

    __tablename__ = "tenant_access_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    cognito_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    proposed_tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    target_login: Mapped[str] = mapped_column(String(255), nullable=False)
    motivation: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)


class Tenant(Base):
    """Thin forward-looking tenant table. tenants.id = organizations.id (1:1)."""

    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("organizations.id"),
        primary_key=True,
    )
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    shared_app_ref: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)

    organization = relationship("Organization", backref="tenant", uselist=False, lazy="selectin")


class TenantMembership(Base):
    """Records a user's membership in a tenant (organization).

    Issue #2961: D5 data foundation — one row per (user, tenant) pair.
    The partial unique index on (user_id) WHERE is_active = true guarantees
    exactly one active tenant per user at the DB level.
    """

    __tablename__ = "tenant_memberships"
    __table_args__ = (UniqueConstraint("user_id", "tenant_id", name="uq_tenant_memberships_user_tenant"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member", server_default="member")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    joined_via: Mapped[str] = mapped_column(String(32), nullable=False, default="org_membership", server_default="org_membership")
    github_org_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)

    # Relationships for ORM navigation
    user = relationship("User", backref="tenant_memberships", lazy="selectin", passive_deletes=True)
    organization = relationship("Organization", backref="memberships", lazy="selectin", passive_deletes=True)
