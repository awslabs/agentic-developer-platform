from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, DateTime, Index, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, new_uuid, utcnow


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    aws_accounts: Mapped[dict] = mapped_column(JSON, nullable=False, default=list)
    role_mappings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    settings: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Issue #375: Identity columns for tenant-identity Phase A
    # Uses JSON type in model (compatible with SQLite for tests); migration uses JSONB with GIN indexes.
    github_installation_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    cognito_client_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Issue #719: Per-tenant policy for auto-approving org members on sign-up.
    # Valid values: "auto_approve_org_members", "require_admin_approval"
    member_approval_policy: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="auto_approve_org_members",
        server_default="auto_approve_org_members",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Department(Base, TenantMixin):
    __tablename__ = "departments"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    budget_limit: Mapped[Decimal | None] = mapped_column(Numeric(precision=15, scale=2))
    # Cognito field - replaces identity_center_group_id
    cognito_group_name: Mapped[str | None] = mapped_column(String(255))
    # Keep legacy field for backward compatibility during migration
    identity_center_group_id: Mapped[str | None] = mapped_column(String(255))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)


class Team(Base, TenantMixin):
    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    department_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Keep legacy field for backward compatibility during migration
    identity_center_group_id: Mapped[str | None] = mapped_column(String(255))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)


class User(Base, TenantMixin):
    __tablename__ = "users"
    __table_args__ = (
        # Issue #700: prevent duplicate canonical rows for the same Cognito identity.
        # Partial unique index — only enforced for non-NULL cognito_sub values.
        Index(
            "uq_users_cognito_sub",
            "cognito_sub",
            unique=True,
            postgresql_where=text("cognito_sub IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    team_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str | None] = mapped_column(String(64))
    # Cognito fields - replaces identity_center_user_id
    cognito_sub: Mapped[str | None] = mapped_column(String(255), index=True)
    cognito_username: Mapped[str | None] = mapped_column(String(255))
    # Keep legacy field for backward compatibility during migration
    identity_center_user_id: Mapped[str | None] = mapped_column(String(255))
    # Issue #446: shadow users are auto-provisioned from channel_tenant_map;
    # they can receive agent messages but cannot log into the ADP UI.
    is_shadow: Mapped[bool] = mapped_column(default=False, server_default="false", nullable=False)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), onupdate=utcnow)


class ServiceAccount(Base, TenantMixin):
    __tablename__ = "service_accounts"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    department_id: Mapped[str] = mapped_column(String(255), nullable=False)
    team_id: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    iam_role_arn: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
