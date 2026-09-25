"""Explicit cluster scopes under the maintained organization grant entity."""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

CLUSTER_USE = "cluster:use"
CLUSTER_ADMINISTER = "cluster:administer"
CLUSTER_OBSERVE = "cluster:observe"
CLUSTER_PERMISSIONS = frozenset({CLUSTER_USE, CLUSTER_ADMINISTER, CLUSTER_OBSERVE})


class OrganizationGrantClusterScope(Base):
    __tablename__ = "organization_grant_cluster_scopes"
    __table_args__ = (
        UniqueConstraint("grant_id", "cluster_id", name="uq_org_grant_cluster_scope"),
        ForeignKeyConstraint(
            ["org_id", "grant_id"],
            ["organization_grants.org_id", "organization_grants.id"],
            name="fk_cluster_scope_org_grant",
        ),
        ForeignKeyConstraint(
            ["org_id", "cluster_id"],
            ["clusters.org_id", "clusters.id"],
            name="fk_cluster_scope_org_cluster",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    grant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    cluster_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    permissions: Mapped[str] = mapped_column(Text, nullable=False)
    generation: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
