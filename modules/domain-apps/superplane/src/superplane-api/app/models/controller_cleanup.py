"""Durable cleanup action ownership; it is evidence, never a mutation grant."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    String,
    Text,
    UniqueConstraint,
    func,
)

from app.database import Base


class ControllerCleanupBinding(Base):
    __tablename__ = "controller_cleanup_bindings"
    __table_args__ = (
        UniqueConstraint("org_id", "workspace_id", "deployment_id"),
        UniqueConstraint("org_id", "workspace_id", "allocation_id"),
        UniqueConstraint("org_id", "workspace_id", "request_id"),
        CheckConstraint("source_fence > 0"),
        {"info": {"postgresql_bootstrap_journal": True}},
    )
    source_operation_id = Column(String(255), primary_key=True)
    org_id = Column(String(255), nullable=False)
    workspace_id = Column(String(255), nullable=False)
    deployment_id = Column(String(36), nullable=False)
    allocation_id = Column(String(255), nullable=False)
    source_plan_digest = Column(String(64), nullable=False)
    source_fence = Column(BigInteger, nullable=False)
    claim_holder = Column(String(255), nullable=False)
    claim_attempt_id = Column(String(255), nullable=False)
    claim_subject = Column(String(255), nullable=False)
    cancel_requested_at = Column(DateTime(timezone=True), nullable=False)
    cancel_requested_by = Column(String(255), nullable=False)
    request_id = Column(String(36), nullable=False)
    request_payload = Column(Text, nullable=False)
    plan_digest = Column(String(64), nullable=False)
    approval_id = Column(String(255), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
