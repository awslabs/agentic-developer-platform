"""Product identity, governed Projects, and immutable operation history."""

from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, CheckConstraint, DateTime, ForeignKeyConstraint, Index, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, new_uuid, utcnow

ProfileJSON = JSON().with_variant(JSONB(), "postgresql")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("revision >= 1"),
        Index("ix_products_created", "tenant_id", "created_at", "id"),
        Index("ix_products_owner", "tenant_id", "owner_user_id", "id"),
        Index("ix_products_team", "tenant_id", "team_id", "id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    intended_users: Mapped[list] = mapped_column(ProfileJSON, nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    team_id: Mapped[str | None] = mapped_column(String(255))
    starting_point: Mapped[str] = mapped_column(String(20), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft", server_default="draft")
    domain: Mapped[str | None] = mapped_column(String(200))
    goals: Mapped[list] = mapped_column(ProfileJSON, nullable=False, default=list)
    provenance: Mapped[list] = mapped_column(ProfileJSON, nullable=False, default=list)
    delivery_home_repository: Mapped[dict | None] = mapped_column(ProfileJSON)
    profile_configuration: Mapped[dict | None] = mapped_column(ProfileJSON)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0", server_default="1.0")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (Index("ix_projects_created", "tenant_id", "created_at", "id"),)

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="registered", server_default="registered")
    configuration: Mapped[dict] = mapped_column(ProfileJSON, nullable=False, default=dict)
    provenance: Mapped[list] = mapped_column(ProfileJSON, nullable=False, default=list)
    execution_policy_ref: Mapped[dict | None] = mapped_column(ProfileJSON)
    budget_policy_ref: Mapped[dict | None] = mapped_column(ProfileJSON)
    budget_meter_initialized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ProductProjectBinding(Base):
    __tablename__ = "product_project_bindings"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from"),
        Index("ix_bindings_product", "tenant_id", "product_id", "valid_to", "project_id"),
        Index(
            "uq_product_base_project",
            "tenant_id",
            "product_id",
            unique=True,
            postgresql_where=text("valid_to IS NULL AND is_base"),
            sqlite_where=text("valid_to IS NULL AND is_base = 1"),
        ),
        Index(
            "uq_bindings_current_project",
            "tenant_id",
            "project_id",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
            sqlite_where=text("valid_to IS NULL"),
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    is_base: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    relationship_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    supersedes_revision: Mapped[int | None] = mapped_column(BigInteger)
    mapping_version: Mapped[str] = mapped_column(String(64), nullable=False, default="profile-registry-1")
    source_connection_id: Mapped[str] = mapped_column(String(255), nullable=False, default="adp.profile")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    provenance: Mapped[dict] = mapped_column(ProfileJSON, nullable=False)


class ProjectRun(Base):
    """Immutable execution attribution, shared by metering and lineage readers."""

    __tablename__ = "project_runs"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
        Index("ix_project_runs_project", "tenant_id", "project_id", "created_at"),
        Index("ix_project_runs_flow", "tenant_id", "flow_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    flow_id: Mapped[str | None] = mapped_column(String(36))
    parent_run_id: Mapped[str | None] = mapped_column(String(255))
    owner_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    relationship_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ProductSourceBinding(Base):
    __tablename__ = "product_source_bindings"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        ForeignKeyConstraint(["installation_id"], ["channel_tenant_map.id"]),
        ForeignKeyConstraint(["credential_id"], ["user_credentials.id"]),
        CheckConstraint("(installation_id IS NOT NULL) != (credential_id IS NOT NULL)"),
        CheckConstraint("(status = 'unlinked') = (unlinked_at IS NOT NULL)"),
        CheckConstraint("status IN ('unverified', 'healthy', 'degraded', 'revoked', 'unlinked')"),
        CheckConstraint("generation >= 1 AND revision >= 1"),
        UniqueConstraint("tenant_id", "project_id", "id"),
        Index("ix_source_bindings_project", "tenant_id", "project_id", "status", "id"),
        Index("ix_source_bindings_installation", "installation_id"),
        Index("ix_source_bindings_credential", "credential_id"),
        Index(
            "uq_source_bindings_active_identity",
            "tenant_id",
            "project_id",
            "kind",
            "provider",
            "namespace",
            "native_id",
            unique=True,
            postgresql_where=text("unlinked_at IS NULL"),
            sqlite_where=text("unlinked_at IS NULL"),
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    installation_id: Mapped[str | None] = mapped_column(String(36))
    credential_id: Mapped[str | None] = mapped_column(String(36))
    namespace: Mapped[str] = mapped_column(String(512), nullable=False)
    native_id: Mapped[str] = mapped_column(String(512), nullable=False)
    desired: Mapped[dict] = mapped_column(ProfileJSON, nullable=False)
    observed: Mapped[dict | None] = mapped_column(ProfileJSON)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="unverified", server_default="unverified")
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    unlinked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ConnectorPolicyRealm(Base):
    __tablename__ = "connector_policy_realms"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        CheckConstraint("revision >= 1"),
        CheckConstraint("state IN ('active', 'retired')"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    revision: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    descriptor_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    descriptor: Mapped[dict] = mapped_column(ProfileJSON, nullable=False)
    source_policy_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    evidence_policy_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectorCell(Base):
    __tablename__ = "connector_cells"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "realm_id", "realm_revision"],
            [
                "connector_policy_realms.tenant_id",
                "connector_policy_realms.project_id",
                "connector_policy_realms.id",
                "connector_policy_realms.revision",
            ],
        ),
        UniqueConstraint("tenant_id", "project_id", "id"),
        UniqueConstraint("tenant_id", "project_id", "realm_id", "realm_revision"),
        CheckConstraint("routing_revision >= 1"),
        CheckConstraint("state IN ('active', 'retired')"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    realm_id: Mapped[str] = mapped_column(String(36), nullable=False)
    realm_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    database_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    role_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    object_prefix: Mapped[str] = mapped_column(String(512), nullable=False)
    workload_id: Mapped[str] = mapped_column(String(255), nullable=False)
    audience: Mapped[str] = mapped_column(String(255), nullable=False)
    routing_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConnectorCollectionGrant(Base):
    __tablename__ = "connector_collection_grants"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "id"),
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "binding_id"],
            ["product_source_bindings.tenant_id", "product_source_bindings.project_id", "product_source_bindings.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "cell_id"], ["connector_cells.tenant_id", "connector_cells.project_id", "connector_cells.id"]
        ),
        CheckConstraint("binding_generation >= 1 AND policy_revision >= 1"),
        CheckConstraint("state IN ('active', 'revoked')"),
        Index("ix_connector_grants_binding", "tenant_id", "binding_id", "state"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    binding_id: Mapped[str] = mapped_column(String(36), nullable=False)
    binding_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    cell_id: Mapped[str] = mapped_column(String(36), nullable=False)
    consumer: Mapped[str] = mapped_column(String(64), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    source_selections: Mapped[list] = mapped_column(ProfileJSON, nullable=False)
    granting_actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ConnectorOperationContext(Base):
    __tablename__ = "connector_operation_contexts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "grant_id"],
            ["connector_collection_grants.tenant_id", "connector_collection_grants.project_id", "connector_collection_grants.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "cell_id"], ["connector_cells.tenant_id", "connector_cells.project_id", "connector_cells.id"]
        ),
        UniqueConstraint("tenant_id", "consumer", "cell_id", "operation_id", "attempt_id", name="uq_connector_context_cell_attempt"),
        CheckConstraint("source_generation >= 1 AND routing_revision >= 1 AND policy_revision >= 1 AND lease_fence >= 1"),
        Index("ix_connector_context_grant", "tenant_id", "grant_id", "expires_at"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    grant_id: Mapped[str] = mapped_column(String(36), nullable=False)
    cell_id: Mapped[str] = mapped_column(String(36), nullable=False)
    delegation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    delegation_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    consumer: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(255), nullable=False)
    lease_fence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    routing_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    policy_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    module_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    context_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ConnectorModuleAdmission(Base):
    __tablename__ = "connector_module_admissions"
    __table_args__ = (ForeignKeyConstraint(["tenant_id"], ["organizations.id"]), CheckConstraint("generation >= 1"))

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, server_default="1")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=text("true"))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ConnectorEvidenceDependency(Base):
    __tablename__ = "connector_evidence_dependencies"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "cell_id"], ["connector_cells.tenant_id", "connector_cells.project_id", "connector_cells.id"]
        ),
        CheckConstraint("revision >= 1"),
        CheckConstraint("state IN ('active', 'revoked')"),
        UniqueConstraint("tenant_id", "project_id", "cell_id", "identity_hash", "revision"),
        Index("ix_connector_dependency_cell", "tenant_id", "project_id", "cell_id", "state"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    cell_id: Mapped[str] = mapped_column(String(36), nullable=False)
    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    identity: Mapped[dict] = mapped_column(ProfileJSON, nullable=False)
    policy_refs: Mapped[list] = mapped_column(ProfileJSON, nullable=False)
    depends_on: Mapped[list] = mapped_column(ProfileJSON, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ProductOperation(Base):
    __tablename__ = "product_operations"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        ForeignKeyConstraint(["tenant_id", "session_id"], ["product_onboarding_sessions.tenant_id", "product_onboarding_sessions.id"]),
        ForeignKeyConstraint(["tenant_id", "binding_id"], ["product_source_bindings.tenant_id", "product_source_bindings.id"]),
        CheckConstraint("(lease_owner IS NULL) = (lease_until IS NULL)"),
        CheckConstraint("attempt_count >= 0 AND attempt_count <= 10"),
        CheckConstraint("callback_sequence >= 0", name="ck_product_operations_callback_sequence"),
        UniqueConstraint("tenant_id", "actor_id", "operation", "key_hash"),
        Index("ix_product_operation_recovery", "state", "retry_at", "lease_until"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str | None] = mapped_column(String(36))
    project_id: Mapped[str | None] = mapped_column(String(255))
    session_id: Mapped[str | None] = mapped_column(String(36))
    binding_id: Mapped[str | None] = mapped_column(String(36))
    binding_generation: Mapped[int | None] = mapped_column(BigInteger)
    expected_revision: Mapped[int | None] = mapped_column(BigInteger)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_operation_id: Mapped[str | None] = mapped_column(String(512))
    callback_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="succeeded")
    request: Mapped[dict] = mapped_column(ProfileJSON, nullable=False)
    receipt: Mapped[dict | None] = mapped_column(ProfileJSON)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


def initial_onboarding_steps() -> dict:
    return {section: {"status": "not_started", "evidence_refs": []} for section in ("information", "basic", "advanced", "financials")}


class ProductOnboardingSession(Base):
    __tablename__ = "product_onboarding_sessions"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
        ForeignKeyConstraint(["tenant_id", "project_id"], ["projects.tenant_id", "projects.id"]),
        CheckConstraint("state IN ('open', 'waiting_external', 'completed', 'cancelled')"),
        CheckConstraint("last_product_revision >= 1"),
        Index("ix_onboarding_actor_state", "tenant_id", "project_id", "actor_user_id", "state", "id"),
        Index(
            "uq_onboarding_active_actor",
            "tenant_id",
            "product_id",
            "project_id",
            "actor_user_id",
            unique=True,
            postgresql_where=text("state IN ('open', 'waiting_external')"),
            sqlite_where=text("state IN ('open', 'waiting_external')"),
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open", server_default="open")
    steps: Mapped[dict] = mapped_column(ProfileJSON, nullable=False, default=initial_onboarding_steps)
    proposals: Mapped[list] = mapped_column(ProfileJSON, nullable=False, default=list)
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False, default="profile-1", server_default="profile-1")
    last_product_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ProductChange(Base):
    __tablename__ = "product_changes"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "product_id"], ["products.tenant_id", "products.id"]),
        ForeignKeyConstraint(["tenant_id", "operation_id"], ["product_operations.tenant_id", "product_operations.id"]),
        UniqueConstraint("tenant_id", "product_id", "revision"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(255))
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    changed_paths: Mapped[list] = mapped_column(ProfileJSON, nullable=False)
    snapshot: Mapped[dict] = mapped_column(ProfileJSON, nullable=False)
    source_revisions: Mapped[list] = mapped_column(ProfileJSON, nullable=False, default=list)
    relationship_versions: Mapped[list] = mapped_column(ProfileJSON, nullable=False, default=list)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ProductSourceSnapshot(Base):
    __tablename__ = "product_source_snapshots"
    __table_args__ = (
        UniqueConstraint("tenant_id", "delegation_id", "idempotency_key"),
        Index("ix_product_source_snapshots_expiry", "expires_at"),
        Index("ix_product_source_snapshots_scope", "tenant_id", "product_id", "project_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    product_id: Mapped[str] = mapped_column(String(36), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    delegation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    delegation_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_ref: Mapped[dict] = mapped_column(ProfileJSON, nullable=False)
    receipt: Mapped[dict] = mapped_column(ProfileJSON, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    retain_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
