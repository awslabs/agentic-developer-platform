"""Stable identity for DevOps jobs found outside ADP admission."""

from sqlalchemy import ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class NativeRunIdentity(Base):
    __tablename__ = "l5_devops_native_run_identities"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    connection_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    space_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    execution_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
