"""User model — org members with RBAC roles."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Valid RBAC roles (ordered by privilege)
VALID_ROLES = ("developer", "workspace-admin", "org-admin")

# User status constants
USER_STATUS_INVITED = "invited"
USER_STATUS_ACTIVE = "active"
USER_STATUS_DISABLED = "disabled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    cognito_sub: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="developer")
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default=USER_STATUS_INVITED
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
