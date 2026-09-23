"""Event model — audit and events (section 6.1)."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Nullable since issue #5673 (A17). An attempt rejected BEFORE any identity was
    # established -- an unauthenticated call, or one refused by the guard before a
    # token was admitted -- has no tenant to attribute, and that is precisely the
    # attempt an audit trail most needs to retain. The alternative to a NULL here is
    # inventing a tenant for an unattributable request or (as the code did before)
    # dropping the record entirely.
    #
    # Consequence, recorded because it is a real limit: `GET /events` is tenant-scoped
    # (`Event.org_id == org_id`), so these rows are deliberately invisible to that API.
    # An unattributed attempt cannot be shown to a tenant without guessing whose it
    # was. Operators read them out-of-band.
    org_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=True
    )
    user_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc=(
            "LEGACY actor column. Before #5673 the audit middleware wrote str(org_id) "
            "here, so historical rows name a TENANT, not a person. New middleware rows "
            "carry the acting principal in `principal` and leave this to other writers. "
            "Kept readable rather than backfilled: the person who acted was never "
            "recorded, so there is nothing to recover it from."
        ),
    )
    principal: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        doc=(
            "WHO acted -- the verified principal (subject), distinct from the tenant in "
            "org_id. PRINCIPAL_UNRESOLVED when no identity was established. NULL marks a "
            "pre-#5673 row, which is what distinguishes historical rows from new ones."
        ),
    )
    outcome: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        doc=(
            "Whether the attempt was ALLOWED or DENIED. NULL on pre-#5673 rows, which "
            "recorded successes only, so NULL must not be read as 'allowed'."
        ),
    )
    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        doc="HTTP method or action verb (created, updated, deleted, read)",
    )
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_ip: Mapped[str | None] = mapped_column(
        String(45), nullable=True, doc="Client IP address"
    )
    request_path: Mapped[str | None] = mapped_column(
        String(512), nullable=True, doc="Full request path"
    )
    http_status: Mapped[int | None] = mapped_column(
        nullable=True, doc="HTTP response status code"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index("ix_events_org_id_created_at", "org_id", "created_at"),
        Index("ix_events_resource_type", "resource_type"),
        Index("ix_events_user_id", "user_id"),
        # "What did this principal attempt, newest first" is the question an incident
        # reviewer actually asks, and it is now answerable without scanning a table
        # that records denials as well as successes.
        Index("ix_events_principal_created_at", "principal", "created_at"),
    )
