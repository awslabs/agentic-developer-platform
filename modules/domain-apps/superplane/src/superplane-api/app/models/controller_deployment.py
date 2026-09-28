"""Immutable deployment-to-paid-operation registration."""

from sqlalchemy import CheckConstraint, Column, DateTime, String, UniqueConstraint, func

from app.database import Base


class ControllerDeploymentOperation(Base):
    __tablename__ = "controller_deployment_operations"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "workspace_id",
            "deployment_id",
            "action",
            name="uq_controller_deployment_action",
        ),
        UniqueConstraint(
            "org_id",
            "workspace_id",
            "request_id",
            name="uq_controller_deployment_request",
        ),
        CheckConstraint("action IN ('provision','teardown')"),
        CheckConstraint(
            "(action='provision' AND source_operation_id='') OR "
            "(action='teardown' AND source_operation_id<>'')"
        ),
        CheckConstraint("plan_digest ~ '^[a-f0-9]{64}$'"),
        CheckConstraint("request_sha256 ~ '^[a-f0-9]{64}$'"),
        CheckConstraint("target_sha256 ~ '^[a-f0-9]{64}$'"),
        {"info": {"postgresql_bootstrap_journal": True}},
    )
    operation_id = Column(String(255), primary_key=True)
    deployment_id = Column(String(36), nullable=False)
    org_id = Column(String(255), nullable=False)
    workspace_id = Column(String(255), nullable=False)
    action = Column(String(16), nullable=False)
    request_id = Column(String(36), nullable=False)
    allocation_id = Column(String(255), nullable=False)
    source_operation_id = Column(String(255), nullable=False)
    plan_digest = Column(String(64), nullable=False)
    request_sha256 = Column(String(64), nullable=False)
    target_sha256 = Column(String(64), nullable=False)
    registered_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
