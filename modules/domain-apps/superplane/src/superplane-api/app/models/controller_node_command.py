"""Metadata for migration 039's original-instance native command evidence."""

from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ControllerNodeCommand(Base):
    __tablename__ = "controller_node_commands"
    __table_args__ = (
        CheckConstraint("purpose IN ('node-bootstrap','node-api-dns-tls')"),
        CheckConstraint(
            "state IN ('prepared','dispatching','accepted','running','succeeded','failed','uncertain')"
        ),
        CheckConstraint("(dispatched_at IS NULL) = (observation_deadline IS NULL)"),
        CheckConstraint("command_id IS NULL OR dispatched_at IS NOT NULL"),
        UniqueConstraint("operation_id", "instance_id", "purpose"),
        Index(
            "ix_controller_node_commands_allocation",
            "org_id",
            "workspace_id",
            "allocation_id",
        ),
    )

    reference: Mapped[str] = mapped_column(Text, primary_key=True)
    operation_id: Mapped[str] = mapped_column(Text, nullable=False)
    org_id: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str] = mapped_column(Text, nullable=False)
    allocation_id: Mapped[str] = mapped_column(Text, nullable=False)
    plan_digest: Mapped[str] = mapped_column(Text, nullable=False)
    step_key: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    instance_id: Mapped[str] = mapped_column(Text, nullable=False)
    region: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[str] = mapped_column(Text, nullable=False)
    contract: Mapped[str] = mapped_column(Text, nullable=False)
    contract_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'prepared'")
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    observation_deadline: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    command_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
