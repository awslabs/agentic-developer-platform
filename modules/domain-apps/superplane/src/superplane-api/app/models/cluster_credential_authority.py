"""Installed cluster-owned credential authority and retained delegation evidence."""

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class ClusterCredentialAuthority(Base):
    __tablename__ = "cluster_credential_authorities"
    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "cluster_id"],
            ["clusters.org_id", "clusters.id"],
            name="fk_cluster_credential_authorities_org",
        ),
        UniqueConstraint(
            "org_id", "cluster_id", name="uq_cluster_credential_authorities_cluster"
        ),
        CheckConstraint(
            "fence_token >= 0", name="ck_cluster_credential_authorities_fence"
        ),
        CheckConstraint(
            "(holder IS NULL) = (lease_expires_at IS NULL)",
            name="ck_cluster_credential_authorities_lease",
        ),
    )
    authority_id = Column(UUID(as_uuid=True), primary_key=True)
    org_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    cluster_id = Column(UUID(as_uuid=True), ForeignKey("clusters.id"), nullable=False)
    document_json = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, server_default=false())
    holder = Column(Text, nullable=True)
    fence_token = Column(BigInteger, nullable=False, server_default="0")
    lease_expires_at = Column(DateTime(timezone=True), nullable=True)


class MembershipCredentialComponent(Base):
    __tablename__ = "membership_credential_components"
    __table_args__ = (
        ForeignKeyConstraint(
            ["membership_id", "revision", "scope"],
            [
                "membership_credentials.membership_id",
                "membership_credentials.revision",
                "membership_credentials.scope",
            ],
        ),
        CheckConstraint(
            "kind IN ('ServiceAccount','Role','RoleBinding')",
            name="ck_membership_credential_components_kind",
        ),
        CheckConstraint(
            "state IN ('planned','created','revoked')",
            name="ck_membership_credential_components_state",
        ),
    )
    membership_id = Column(UUID(as_uuid=True), primary_key=True)
    revision = Column(Integer, primary_key=True)
    scope = Column(String(16), primary_key=True)
    kind = Column(String(32), primary_key=True)
    name = Column(String(253), primary_key=True)
    desired_json = Column(Text, nullable=False)
    identity_json = Column(Text, nullable=True)
    state = Column(String(16), nullable=False, server_default="planned")
