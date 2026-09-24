"""Explicit human decisions bound to the exact request admitted by the harness."""

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


class OperationApproval(Base):
    __tablename__ = "operation_approvals"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "workspace_id",
            "requester",
            "plan_digest",
            name="uq_operation_approval_request",
        ),
    )

    approval_id = Column(String(64), primary_key=True)
    # No workspace FK: approval precedes creation of the first workspace.
    org_id = Column(String(255), nullable=False)
    workspace_id = Column(String(255), nullable=False)
    requester = Column(String(255), nullable=False)
    plan_digest = Column(String(64), nullable=False)
    request_payload = Column(Text, nullable=False)
    approvers_json = Column(Text, nullable=False)
    max_resource_units = Column(BigInteger, nullable=False)
    max_runtime_seconds = Column(BigInteger, nullable=False)
    max_cost_micros = Column(BigInteger, nullable=False)
    result = Column(String(32), nullable=True)
    decided_by = Column(String(255), nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked = Column(Boolean, nullable=False, default=False)
    organization_scope = Column(Boolean, nullable=False, default=False)


class OperationSettlementReceipt(Base):
    __tablename__ = "operation_settlement_receipts"

    receipt_id = Column(String(128), primary_key=True)
    operation_id = Column(String(255), nullable=False, unique=True)
    reservation_id = Column(String(128), nullable=False)
    payload_digest = Column(String(64), nullable=False)
    payload_json = Column(Text, nullable=False)
    disposition = Column(String(32), nullable=False)
    accepted_at = Column(DateTime(timezone=True), nullable=False)
