"""Register the PostgreSQL network journal schema for migration autogeneration."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    String,
    Text,
    text,
)

from app.database import Base


class ControllerNetworkCompletion(Base):
    __tablename__ = "controller_network_completion"
    __table_args__ = ({"info": {"postgresql_only": True}},)
    operation_id = Column(Text, primary_key=True)
    allocation_id = Column(Text, nullable=False)
    plan_digest = Column(Text, nullable=False)
    compute_region = Column(Text, nullable=False)
    completed_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )


class ControllerNetworkResource(Base):
    __tablename__ = "controller_network_resources"
    __table_args__ = (
        CheckConstraint("state IN ('intended','present','delete_intended','absent')"),
        {"info": {"postgresql_only": True}},
    )
    resource_key = Column(Text, primary_key=True)
    org_id = Column(Text, nullable=False)
    descriptor = Column(Text, nullable=False)
    generation = Column(BigInteger, nullable=False, server_default=text("1"))
    state = Column(Text, nullable=False)
    owned = Column(Boolean, nullable=False)
    provider_reference = Column(Text)
    created_by_operation = Column(Text, nullable=False)


class ControllerNetworkMember(Base):
    __tablename__ = "controller_network_members"
    __table_args__ = (
        CheckConstraint("membership_generation ~ '^[a-f0-9]{64}$'"),
        {"info": {"postgresql_only": True}},
    )
    resource_key = Column(
        Text, ForeignKey("controller_network_resources.resource_key"), primary_key=True
    )
    allocation_id = Column(Text, primary_key=True)
    org_id = Column(Text, nullable=False)
    workspace_id = Column(Text, nullable=False)
    cluster_id = Column(Text, nullable=False)
    membership_generation = Column(String(64), nullable=False)
    source_operation_id = Column(Text, nullable=False)
    source_plan_digest = Column(Text, nullable=False)
    released_at = Column(DateTime(timezone=True))


class ControllerNetworkEffect(Base):
    __tablename__ = "controller_network_effects"
    __table_args__ = ({"info": {"postgresql_only": True}},)
    resource_key = Column(
        Text, ForeignKey("controller_network_resources.resource_key"), primary_key=True
    )
    generation = Column(BigInteger, primary_key=True)
    action = Column(Text, primary_key=True)
    operation_id = Column(Text, nullable=False)
    attempt_id = Column(Text, nullable=False)
    fence_token = Column(BigInteger, nullable=False)
    descriptor = Column(Text, nullable=False)
    result = Column(Text)
    intended_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    confirmed_at = Column(DateTime(timezone=True))
