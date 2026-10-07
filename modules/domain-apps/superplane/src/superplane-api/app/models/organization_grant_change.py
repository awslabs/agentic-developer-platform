"""Durable tenant-bound request and audit identities for organization mutations."""

import uuid

from sqlalchemy import (
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OrganizationGrantChange(Base):
    __tablename__ = "organization_grant_changes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "grant_id"],
            ["organization_grants.org_id", "organization_grants.id"],
            name="fk_org_grant_change_tenant",
        ),
        UniqueConstraint("org_id", "request_id", name="uq_org_grant_change_request"),
        UniqueConstraint("grant_id", "revision", name="uq_org_grant_change_revision"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    grant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False
    )
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
