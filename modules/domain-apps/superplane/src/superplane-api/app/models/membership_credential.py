"""Non-secret evidence for each membership's scoped credential revision."""

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID

from app.database import Base


class MembershipCredential(Base):
    __tablename__ = "membership_credentials"
    __table_args__ = (
        CheckConstraint(
            "revision BETWEEN 1 AND 2147483647",
            name="ck_membership_credentials_revision",
        ),
        CheckConstraint(
            "scope IN ('reader','mutator')", name="ck_membership_credentials_scope"
        ),
        CheckConstraint(
            "state IN ('reserved','issued','projected','active','revoking','revoked')",
            name="ck_membership_credentials_state",
        ),
        CheckConstraint(
            "state IN ('reserved','revoking','revoked') OR (service_account_uid IS NOT NULL AND expires_at IS NOT NULL)",
            name="ck_membership_credentials_issued",
        ),
        CheckConstraint(
            "state NOT IN ('projected','active') OR (projection_uid IS NOT NULL AND projection_version IS NOT NULL)",
            name="ck_membership_credentials_projected",
        ),
        CheckConstraint(
            "state <> 'active' OR observed_at IS NOT NULL",
            name="ck_membership_credentials_observed",
        ),
        Index(
            "uq_membership_credentials_active",
            "membership_id",
            "scope",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    membership_id = Column(
        UUID(as_uuid=True), ForeignKey("cluster_memberships.id"), primary_key=True
    )
    revision = Column(Integer, primary_key=True)
    scope = Column(String(16), primary_key=True)
    namespace_uid = Column(String(255), nullable=False)
    service_account_uid = Column(String(255), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    state = Column(String(16), nullable=False, server_default="reserved")
    projection_uid = Column(String(255), nullable=True)
    projection_version = Column(String(255), nullable=True)
    observed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
