import uuid
from datetime import UTC, datetime

from sqlalchemy import String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TenantMixin:
    """Mixin that adds org_id for tenant isolation. All tenant-scoped tables must include this."""

    org_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())
