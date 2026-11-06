"""Metadata-only private native credential state, separate from tool execution results."""

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, utcnow


class NativeCredentialRequest(Base, TenantMixin):
    __tablename__ = "native_credential_requests"
    __table_args__ = (UniqueConstraint("org_id", "user_id", "request_id", name="uq_native_credential_request"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    team_id: Mapped[str] = mapped_column(String(255), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    execution_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    selection: Mapped[dict] = mapped_column(JSON, nullable=False)
    offer: Mapped[dict | None] = mapped_column(JSON)
    snapshot: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    browser_session_id: Mapped[str | None] = mapped_column(String(64))
    approval_nonce_hash: Mapped[str | None] = mapped_column(String(64))
    approved_snapshot: Mapped[str | None] = mapped_column(String(64))
    challenge: Mapped[str | None] = mapped_column(String(64))
    challenge_expires_at: Mapped[int | None] = mapped_column(Integer)
    issued_at: Mapped[int | None] = mapped_column(Integer)
    credential_expires_at: Mapped[int | None] = mapped_column(Integer)
    envelope_digest: Mapped[str | None] = mapped_column(String(64))
    acknowledgment: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
