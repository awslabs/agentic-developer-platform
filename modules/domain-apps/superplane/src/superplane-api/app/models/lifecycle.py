"""Private immutable workspace artifact lineage; no provider credential material."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)

from app.database import Base


class WorkspaceLifecycleArtifact(Base):
    __tablename__ = "workspace_lifecycle_artifacts"
    __table_args__ = (
        CheckConstraint("artifact_id ~ '^[a-f0-9]{64}$'"),
        CheckConstraint("request_revision ~ '^[a-f0-9]{64}$'"),
        CheckConstraint("account_id ~ '^[0-9]{12}$'"),
        CheckConstraint("jsonb_typeof(target_json::jsonb)='object'"),
        CheckConstraint("jsonb_typeof(parameters_json::jsonb)='object'"),
        CheckConstraint("jsonb_typeof(artifact_metadata_json::jsonb)='object'"),
        Index(
            "ix_workspace_lifecycle_artifacts_source",
            "org_id",
            "workspace_id",
            "source_operation_id",
        ),
        {"info": {"postgresql_bootstrap_journal": True}},
    )
    artifact_id = Column(String(64), primary_key=True)
    org_id = Column(String(255), nullable=False)
    workspace_id = Column(String(255), nullable=False)
    source_operation_id = Column(String(255), nullable=False)
    source_job_id = Column(String(255), nullable=False)
    source_attempt_id = Column(String(255), nullable=False)
    source_payload_digest = Column(String(64), nullable=False)
    source_request_payload = Column(Text, nullable=False)
    producer_holder = Column(String(255), nullable=False)
    producer_attempt_id = Column(String(255), nullable=False)
    producer_fence_token = Column(BigInteger, nullable=False)
    request_revision = Column(String(64), nullable=False)
    account_id = Column(String(12), nullable=False)
    target_json = Column(Text, nullable=False)
    parameters_json = Column(Text, nullable=False)
    artifact_metadata_json = Column(Text, nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkspaceLifecycleEffect(Base):
    __tablename__ = "workspace_lifecycle_effects"
    __table_args__ = (
        CheckConstraint("event IN ('intended','confirmed')"),
        CheckConstraint("event_digest ~ '^[a-f0-9]{64}$'"),
        CheckConstraint("jsonb_typeof(descriptor_json::jsonb)='object'"),
        CheckConstraint(
            "(event='intended' AND result_json::jsonb='null'::jsonb) OR (event='confirmed' AND jsonb_typeof(result_json::jsonb)='object')"
        ),
        {"info": {"postgresql_bootstrap_journal": True}},
    )
    org_id = Column(String(255), primary_key=True)
    workspace_id = Column(String(255), primary_key=True)
    operation_id = Column(String(255), primary_key=True)
    phase = Column(String(64), primary_key=True)
    effect_key = Column(String(200), primary_key=True)
    event = Column(String(16), primary_key=True)
    source_job_id = Column(String(255), nullable=False)
    source_payload_digest = Column(String(64), nullable=False)
    recipe_digest = Column(String(64), nullable=False)
    descriptor_digest = Column(String(64), nullable=False)
    descriptor_json = Column(Text(), nullable=False)
    holder = Column(String(255), nullable=False)
    attempt_id = Column(String(255), nullable=False)
    fence_token = Column(BigInteger(), nullable=False)
    result_json = Column(Text(), nullable=False)
    event_digest = Column(String(64), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkspaceLifecycleControlOperation(Base):
    __tablename__ = "workspace_lifecycle_control_operations"
    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "workspace_id",
            "source_bootstrap_operation_id",
            "phase",
            "request_id",
            name="uq_lifecycle_control_request",
        ),
        CheckConstraint("phase='prepare-retirement-access'"),
        CheckConstraint("allocation_id<>original_allocation_id"),
        CheckConstraint("plan_digest ~ '^[a-f0-9]{64}$'"),
        {"info": {"postgresql_bootstrap_journal": True}},
    )
    operation_id = Column(String(255), primary_key=True)
    org_id = Column(String(255), nullable=False)
    workspace_id = Column(String(255), nullable=False)
    source_bootstrap_operation_id = Column(String(255), nullable=False)
    phase = Column(String(64), nullable=False)
    request_id = Column(String(36), nullable=False)
    allocation_id = Column(String(255), nullable=False)
    original_allocation_id = Column(String(255), nullable=False)
    plan_digest = Column(String(64), nullable=False)
    registered_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
