"""Native finding identities scoped to one immutable assessment result."""

from sqlalchemy import ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class Finding(Base):
    __tablename__ = "l5_service_findings"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "result_id"],
                             ["l5_service_results.tenant_id", "l5_service_results.result_id"]),
        UniqueConstraint("tenant_id", "result_id", "kind", "native_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    finding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    result_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    native_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(Payload, nullable=False)
    triage_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
