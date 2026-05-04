"""Audit log model for security-sensitive events.

Issue #446: Vault Phase 2b — Magic-link identity linking flow

Records every magic-link consume attempt (success or failure) and other
security-relevant events (shadow-user creation, identity link/unlink).

Note: The table is named `security_audit_logs` — not `audit_logs` — because
an unrelated admin audit log (admin/models.py::AuditLog) already owns
`audit_logs` since migration 001. These two audit streams are kept separate
because their schemas differ (admin tracks CRUD actions; this tracks
security events like magic-link consume).
"""

from datetime import datetime

from sqlalchemy import JSON, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, new_uuid, utcnow


class AuditLog(Base, TenantMixin):
    """Append-only audit log for vault / identity events.

    ``event_type`` examples:
        magic_link_issued        — token generated and returned
        magic_link_consumed      — nonce successfully consumed, identity linked
        magic_link_failed        — consume attempt rejected (replay/expired/mismatch)
        identity_linked          — user_identities row created
        shadow_user_created      — auto-provisioned shadow user
        identity_unlinked        — user_identities row removed (via Phase 2a DELETE)
    """

    __tablename__ = "security_audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # actor_id: the internal user_id performing the action; None for Lambda-initiated events.
    actor_id: Mapped[str | None] = mapped_column(String(255), index=True)
    # details: arbitrary JSON bag — provider, provider_user_id, jti, error, etc.
    details: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
