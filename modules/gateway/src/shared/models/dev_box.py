"""Canonical owner-scoped Dev Box records shared by the host lifecycle stories."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, new_uuid, utcnow


class DevBoxPolicy(Base):
    __tablename__ = "dev_box_policies"
    __table_args__ = (UniqueConstraint("org_id", "deployment_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    org_id: Mapped[str] = mapped_column(String(255), ForeignKey("organizations.id"), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(default=False, nullable=False)
    max_live_boxes: Mapped[int] = mapped_column(Integer, nullable=False)
    max_running_boxes: Mapped[int] = mapped_column(Integer, nullable=False)
    allowed_sizes: Mapped[list] = mapped_column(JSON, nullable=False)
    max_disk_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    max_storage_gb: Mapped[int | None] = mapped_column(Integer)
    max_running_hours: Mapped[int | None] = mapped_column(Integer)
    instance_hour_rate_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 6))
    disk_gb_hour_rate_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 6))
    snapshot_gb_hour_rate_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 6))
    max_lease_minutes: Mapped[int | None] = mapped_column(Integer)
    idle_stop_minutes: Mapped[int | None] = mapped_column(Integer)
    budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 6))


class DevBoxTemplate(Base):
    __tablename__ = "dev_box_template_versions"
    __table_args__ = (UniqueConstraint("name", "revision"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[str] = mapped_column(String(64), nullable=False)
    profile: Mapped[str] = mapped_column(String(128), nullable=False)
    instance_size: Mapped[str] = mapped_column(String(64), nullable=False)
    disk_gb: Mapped[int] = mapped_column(Integer, nullable=False)
    image_ref: Mapped[str | None] = mapped_column(String(255))
    tools_revision: Mapped[str | None] = mapped_column(String(128))
    approved: Mapped[bool] = mapped_column(default=False, nullable=False)


class DevBox(Base):
    __tablename__ = "dev_boxes"
    __table_args__ = (
        UniqueConstraint("id", "user_id", "org_id", "deployment_id", name="uq_dev_boxes_scope"),
        Index(
            "uq_dev_boxes_live_owner",
            "user_id",
            "org_id",
            "deployment_id",
            unique=True,
            postgresql_where=text("status != 'released'"),
            sqlite_where=text("status != 'released'"),
        ),
        CheckConstraint("machine_generation >= 1", name="ck_dev_boxes_generation"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.id"), nullable=False)
    org_id: Mapped[str] = mapped_column(String(255), ForeignKey("organizations.id"), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    template_id: Mapped[str] = mapped_column(String(36), ForeignKey("dev_box_template_versions.id"), nullable=False)
    template_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    profile: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False, default="assistant", server_default="assistant")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="requested")
    desired_state: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    observed_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    machine_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    auth_readiness: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown", server_default="unknown")
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assistant_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assistant_consolidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assistant_busy_until_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ScopedBoxRecord:
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    org_id: Mapped[str] = mapped_column(String(255), nullable=False)
    deployment_id: Mapped[str] = mapped_column(String(128), nullable=False)
    machine_generation: Mapped[int] = mapped_column(Integer, nullable=False)


class DevBoxInstance(ScopedBoxRecord, Base):
    __tablename__ = "dev_box_instances"
    __table_args__ = (
        ForeignKeyConstraint(
            ["box_id", "user_id", "org_id", "deployment_id"], ["dev_boxes.id", "dev_boxes.user_id", "dev_boxes.org_id", "dev_boxes.deployment_id"]
        ),
        UniqueConstraint("box_id", "machine_generation"),
        UniqueConstraint("id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation", name="uq_dev_box_instance_scope"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    box_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider_instance_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    provider_account_ref: Mapped[str | None] = mapped_column(String(128))
    region: Mapped[str | None] = mapped_column(String(64))
    availability_zone: Mapped[str | None] = mapped_column(String(64))
    network_ref: Mapped[str | None] = mapped_column(String(255))
    private_address: Mapped[str | None] = mapped_column(String(64))
    image_ref: Mapped[str | None] = mapped_column(String(255))
    instance_size: Mapped[str | None] = mapped_column(String(64))
    device_identity_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("user_identities.id"))
    template_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    readiness_class: Mapped[str | None] = mapped_column(String(64))
    error_class: Mapped[str | None] = mapped_column(String(64))
    enrollment_public_key: Mapped[bytes | None] = mapped_column(LargeBinary)
    certificate_fingerprint: Mapped[str | None] = mapped_column(String(64))
    # Release the GENERATION was provisioned from (#8223 cause 34): a same-generation
    # restart is verified against these, never against the release that is current when the
    # box comes back. Nullable for rows written before the migration; the controller fills
    # them from the host's first-boot receipt on the next verification.
    profile_revision: Mapped[str | None] = mapped_column(String(64))
    source_revision: Mapped[str | None] = mapped_column(String(64))
    crew_bundle_sha256: Mapped[str | None] = mapped_column(String(64))
    launch_template_version: Mapped[str | None] = mapped_column(String(32))


class DevBoxResource(ScopedBoxRecord, Base):
    __tablename__ = "dev_box_resources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["box_id", "user_id", "org_id", "deployment_id"], ["dev_boxes.id", "dev_boxes.user_id", "dev_boxes.org_id", "dev_boxes.deployment_id"]
        ),
        UniqueConstraint("id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation", name="uq_dev_box_resource_scope"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    box_id: Mapped[str] = mapped_column(String(36), nullable=False)
    resource_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    size_gb: Mapped[int | None] = mapped_column(Integer)
    encrypted: Mapped[bool | None] = mapped_column()
    snapshot_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_state: Mapped[str] = mapped_column(String(32), nullable=False, default="retained")
    deletion_state: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_observed_state: Mapped[str | None] = mapped_column(String(32))
    last_rate_usd: Mapped[Decimal | None] = mapped_column(Numeric(15, 6))
    coverage_gap_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DevBoxOperation(ScopedBoxRecord, Base):
    __tablename__ = "dev_box_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["box_id", "user_id", "org_id", "deployment_id"], ["dev_boxes.id", "dev_boxes.user_id", "dev_boxes.org_id", "dev_boxes.deployment_id"]
        ),
        UniqueConstraint("user_id", "org_id", "deployment_id", "idempotency_key", name="uq_dev_box_operation_key"),
        UniqueConstraint("id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation", name="uq_dev_box_operation_scope"),
        CheckConstraint("status IN ('pending', 'unknown', 'succeeded', 'failed')", name="ck_dev_box_operation_status"),
        Index(
            "uq_dev_box_active_operation",
            "box_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'unknown')"),
            sqlite_where=text("status IN ('pending', 'unknown')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    box_id: Mapped[str] = mapped_column(String(36), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[str] = mapped_column(String(255), ForeignKey("users.id"), nullable=False)
    provider_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    retention_choice: Mapped[str | None] = mapped_column(String(16))
    provider_request_ref: Mapped[str | None] = mapped_column(String(255))
    progress: Mapped[str] = mapped_column(String(64), nullable=False, default="admitted")
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_class: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DevBoxEvent(ScopedBoxRecord, Base):
    __tablename__ = "dev_box_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["box_id", "user_id", "org_id", "deployment_id"], ["dev_boxes.id", "dev_boxes.user_id", "dev_boxes.org_id", "dev_boxes.deployment_id"]
        ),
        ForeignKeyConstraint(
            ["operation_id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation"],
            [
                "dev_box_operations.id",
                "dev_box_operations.box_id",
                "dev_box_operations.user_id",
                "dev_box_operations.org_id",
                "dev_box_operations.deployment_id",
                "dev_box_operations.machine_generation",
            ],
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    box_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class DevBoxUsage(ScopedBoxRecord, Base):
    __tablename__ = "dev_box_usage"
    __table_args__ = (
        ForeignKeyConstraint(
            ["box_id", "user_id", "org_id", "deployment_id"], ["dev_boxes.id", "dev_boxes.user_id", "dev_boxes.org_id", "dev_boxes.deployment_id"]
        ),
        ForeignKeyConstraint(
            ["resource_id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation"],
            [
                "dev_box_resources.id",
                "dev_box_resources.box_id",
                "dev_box_resources.user_id",
                "dev_box_resources.org_id",
                "dev_box_resources.deployment_id",
                "dev_box_resources.machine_generation",
            ],
        ),
        UniqueConstraint("resource_id", "interval_start", "interval_end", "source_revision", name="uq_dev_box_usage_interval"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    box_id: Mapped[str] = mapped_column(String(36), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(36), nullable=False)
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(15, 6), nullable=False)
    units: Mapped[Decimal] = mapped_column(Numeric(15, 6), nullable=False)
    unit_name: Mapped[str] = mapped_column(String(32), nullable=False)
    rate_usd: Mapped[Decimal] = mapped_column(Numeric(15, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    reconciled: Mapped[bool] = mapped_column(default=False, nullable=False)
    source_revision: Mapped[str] = mapped_column(String(128), nullable=False)


class DevBoxDevice(ScopedBoxRecord, Base):
    __tablename__ = "dev_box_devices"
    __table_args__ = (
        ForeignKeyConstraint(
            ["box_id", "user_id", "org_id", "deployment_id"], ["dev_boxes.id", "dev_boxes.user_id", "dev_boxes.org_id", "dev_boxes.deployment_id"]
        ),
        ForeignKeyConstraint(
            ["instance_id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation"],
            [
                "dev_box_instances.id",
                "dev_box_instances.box_id",
                "dev_box_instances.user_id",
                "dev_box_instances.org_id",
                "dev_box_instances.deployment_id",
                "dev_box_instances.machine_generation",
            ],
        ),
        UniqueConstraint("box_id", "machine_generation", "public_key_digest"),
        UniqueConstraint("id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation", name="uq_dev_box_device_scope"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    box_id: Mapped[str] = mapped_column(String(36), nullable=False)
    instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    public_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    enrollment_digest: Mapped[str | None] = mapped_column(String(64), unique=True)
    sealed_enrollment: Mapped[bytes | None] = mapped_column(LargeBinary)
    enrollment_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrollment_consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DevBoxAccessSession(ScopedBoxRecord, Base):
    __tablename__ = "dev_box_access_sessions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["box_id", "user_id", "org_id", "deployment_id"], ["dev_boxes.id", "dev_boxes.user_id", "dev_boxes.org_id", "dev_boxes.deployment_id"]
        ),
        ForeignKeyConstraint(
            ["device_id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation"],
            [
                "dev_box_devices.id",
                "dev_box_devices.box_id",
                "dev_box_devices.user_id",
                "dev_box_devices.org_id",
                "dev_box_devices.deployment_id",
                "dev_box_devices.machine_generation",
            ],
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    box_id: Mapped[str] = mapped_column(String(36), nullable=False)
    device_id: Mapped[str] = mapped_column(String(36), nullable=False)
    provider_session_ref: Mapped[str | None] = mapped_column(String(255))
    certificate_digest: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
