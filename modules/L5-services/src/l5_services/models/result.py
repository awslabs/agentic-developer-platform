"""Immutable public projection of a collected provider result."""

from sqlalchemy import ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class ResultDetail(Base):
    __tablename__ = "l5_service_result_details"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "result_id"],
                             ["l5_service_results.tenant_id", "l5_service_results.result_id"]),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    result_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    payload: Mapped[dict] = mapped_column(Payload, nullable=False)
