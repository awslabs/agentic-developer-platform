"""Action Provenance model.

Records every agent/human action for correlation tracking and audit.
Part of Phase 2-a (#784) — storage only, no runtime reads/writes yet.
"""

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, TenantMixin, new_uuid, utcnow


class ActionProvenance(Base, TenantMixin):
    """Tracks provenance of every action in an agent/human chain.

    UUID generation:
    - Primary: Python-side uuid4() via `new_uuid` (used by SQLAlchemy inserts).
    - Belt-and-suspenders: Postgres DEFAULT gen_random_uuid() in the migration DDL
      covers raw SQL inserts during debugging. Requires pgcrypto extension.

    JSON column note:
    - The model uses SQLAlchemy `JSON` (not `JSONB`) for SQLite test compatibility.
    - The migration DDL uses JSONB for Postgres performance (GIN indexable later).
    - If a GIN index on source_event is needed, add it in a future migration.
    """

    __tablename__ = "action_provenance"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    actor_user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.id"), nullable=False)
    triggered_by: Mapped[str | None] = mapped_column(String(255), ForeignKey("users.id"), nullable=True)
    root_human_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.id"), nullable=False)
    is_human_rooted: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    action_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source_event: Mapped[dict] = mapped_column(JSON, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
