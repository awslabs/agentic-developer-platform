"""PostgreSQL bootstrap journals, operated by the domain's raw-SQL adapters.

Register their exact schema for Alembic autogeneration. These records precede
workspace registration and deliberately have no workspace foreign key.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Computed,
    DateTime,
    Index,
    String,
    BigInteger,
    Text,
    false,
    func,
)
from app.database import Base


class WorkspaceBootstrapReservation(Base):
    __tablename__ = "workspace_bootstrap_reservations"
    __table_args__ = (
        CheckConstraint(
            "state IN ('reserved', 'registered')",
            name="ck_workspace_bootstrap_reservations_state",
        ),
        CheckConstraint(
            "workspace_id !~ '^[[:space:]]*$'",
            name="ck_workspace_bootstrap_reservations_workspace_id",
        ),
        CheckConstraint(
            "attempt_token !~ '^[[:space:]]*$'",
            name="ck_workspace_bootstrap_reservations_attempt_token",
        ),
        Index("ix_workspace_bootstrap_reservations_org_id", "org_id"),
        Index("ix_workspace_bootstrap_reservations_state", "state"),
        {"info": {"postgresql_bootstrap_journal": True}},
    )
    workspace_id = Column(String(255), primary_key=True)
    state = Column(String(32), nullable=False)
    identity_json = Column(Text, nullable=False)
    attempt_token = Column(String(128), nullable=False)
    org_id = Column(
        Text,
        Computed("(identity_json::jsonb ->> 'org_id')", persisted=True),
        nullable=False,
    )
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkspaceBootstrapAuthority(Base):
    __tablename__ = "workspace_bootstrap_authority"
    __table_args__ = (
        CheckConstraint("generation ~ '^[a-f0-9]{64}$'"),
        CheckConstraint("claim ~ '^[a-f0-9]{64}$'"),
        CheckConstraint("operation_id !~ '^[[:space:]]*$'"),
        CheckConstraint("org_id !~ '^[[:space:]]*$'"),
        CheckConstraint("jsonb_typeof(plan_json::jsonb) = 'object'"),
        CheckConstraint("jsonb_typeof(progress_json::jsonb) = 'object'"),
        {"info": {"postgresql_bootstrap_journal": True}},
    )
    workspace_id = Column(String(255), primary_key=True)
    generation = Column(String(64), primary_key=True)
    operation_id = Column(Text, nullable=False)
    org_id = Column(Text, nullable=False)
    cluster_arn = Column(Text, nullable=False)
    claim = Column(String(64), nullable=False)
    plan_json = Column(Text, nullable=False)
    progress_json = Column(Text, nullable=False)
    revoked = Column(Boolean, nullable=False, server_default=false())


class WorkspaceBootstrapReadToken(Base):
    """One replaceable, hashed read capability for an actual reservation."""

    __tablename__ = "workspace_bootstrap_read_tokens"
    workspace_id = Column(String(255), primary_key=True)
    org_id = Column(String(255), nullable=False)
    operation_id = Column(String(255), nullable=False)
    registration_claim = Column(String(64), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    lease_holder = Column(String(255), nullable=False)
    lease_attempt_id = Column(String(255), nullable=False)
    lease_fence_token = Column(BigInteger, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
