"""Durable caller-scoped receipts for publication-only retry."""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class PublicationRetryReceipt(Base):
    __tablename__ = "l5_service_publication_retry_receipts"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    service_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    publication_id: Mapped[str] = mapped_column(String(36), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict] = mapped_column(Payload, nullable=False)
