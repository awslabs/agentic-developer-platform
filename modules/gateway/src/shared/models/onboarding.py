"""Onboarding models: tenant access requests and tenants.

Issue #538: Onboarding flow — GitHub sign-in to tenant + user creation.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
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
