"""Fresh decisions for an original admission; its approval is never edited."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    String,
    Text,
    UniqueConstraint,
)

from app.database import Base


class OperationReauthorization(Base):
    __tablename__ = "operation_reauthorizations"
    __table_args__ = (UniqueConstraint("org_id", "idempotency_key"),)

    reauthorization_id = Column(String(64), primary_key=True)
    idempotency_key = Column(String(255), nullable=False)
    original_approval_id = Column(String(64), nullable=False)
    operation_id = Column(String(255), nullable=False)
    org_id = Column(String(255), nullable=False)
    workspace_id = Column(String(255), nullable=False)
    requester = Column(String(255), nullable=False)
    approved_by = Column(String(255), nullable=False)
    plan_digest = Column(String(64), nullable=False)
    expected_version = Column(BigInteger, nullable=False)
    scope = Column(
        String(32),
        nullable=False,
        default="undispatched",
        server_default="undispatched",
    )
    amendment_json = Column(Text, nullable=True)
    result = Column(String(32), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)
