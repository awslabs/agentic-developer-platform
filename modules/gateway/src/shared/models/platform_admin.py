"""Explicit global authority decisions, retained independently of tenant rows."""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, utcnow


class PlatformAdminGrant(Base):
    __tablename__ = "platform_admin_grants"

    # The gateway authenticates against one configured Cognito pool. A subject
    # is a login identity, never a tenant membership, email, or external alias.
    cognito_sub: Mapped[str] = mapped_column(String(255), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
