"""User model — org members with RBAC roles."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func
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
    __table_args__ = (
        # Issue #6127 (DESIGN.md 2.2/4.1). Scoped to (org_id, cognito_sub), not a
        # bare unique on cognito_sub: the ADP identity model allows one human
        # subject to hold organization-local membership in more than one
        # organization, and a global unique constraint made that unrepresentable
        # in this table — a second organization's row for the same subject
        # collided regardless of org_id. Migration 036 narrows the applied index
        # to match; this declaration is what `alembic revision --autogenerate`
        # compares the live database against, so it must agree with the
        # migration or autogenerate keeps proposing to "fix" the difference.
        Index("ix_users_org_cognito_sub", "org_id", "cognito_sub", unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    cognito_sub: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
