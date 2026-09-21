"""Organization management authority, independent of workspace access."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

ORGANIZATION_ADMINISTER = "organization:administer"
ORGANIZATION_READ = "organization:read"


class OrganizationGrantRecord(Base):
    __tablename__ = "organization_grants"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "principal", name="uq_organization_grants_principal"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    principal: Mapped[str] = mapped_column(String(255), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    permissions: Mapped[str] = mapped_column(Text, nullable=False)
    granted_by: Mapped[str] = mapped_column(String(255), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def permission_values(self) -> set[str]:
        return set(self.permissions.split())
