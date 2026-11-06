"""Canonical scoped Dev Box storage and transactional admission fences (#7368)."""

import sqlalchemy as sa

from alembic import op

revision = "088_dev_box_contract"
down_revision = "087_run_report_spool_document"
branch_labels = None
depends_on = None


def scope_columns():
    return [
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("org_id", sa.String(255), nullable=False),
        sa.Column("deployment_id", sa.String(128), nullable=False),
        sa.Column("machine_generation", sa.Integer(), nullable=False),
    ]


def scope_fk():
    return sa.ForeignKeyConstraint(
        ["box_id", "user_id", "org_id", "deployment_id"],
        ["dev_boxes.id", "dev_boxes.user_id", "dev_boxes.org_id", "dev_boxes.deployment_id"],
    )


def owned_fk(column, table):
    return sa.ForeignKeyConstraint(
        [column, "box_id", "user_id", "org_id", "deployment_id", "machine_generation"],
        [f"{table}.id", f"{table}.box_id", f"{table}.user_id", f"{table}.org_id", f"{table}.deployment_id", f"{table}.machine_generation"],
    )


def upgrade():
    op.create_table(
        "dev_box_policies",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(255), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("deployment_id", sa.String(128), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("max_live_boxes", sa.Integer(), nullable=False),
        sa.Column("max_running_boxes", sa.Integer(), nullable=False),
        sa.Column("allowed_sizes", sa.JSON(), nullable=False),
        sa.Column("max_disk_gb", sa.Integer(), nullable=False),
        sa.Column("max_lease_minutes", sa.Integer()),
        sa.Column("idle_stop_minutes", sa.Integer()),
        sa.Column("budget_usd", sa.Numeric(15, 6)),
        sa.UniqueConstraint("org_id", "deployment_id"),
    )
    op.create_table(
        "dev_box_template_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("profile", sa.String(128), nullable=False),
        sa.Column("instance_size", sa.String(64), nullable=False),
        sa.Column("disk_gb", sa.Integer(), nullable=False),
        sa.Column("image_ref", sa.String(255)),
        sa.Column("tools_revision", sa.String(128)),
        sa.Column("approved", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("name", "revision"),
    )
    op.create_table(
        "dev_boxes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(255), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("org_id", sa.String(255), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("deployment_id", sa.String(128), nullable=False),
        sa.Column("template_id", sa.String(36), sa.ForeignKey("dev_box_template_versions.id"), nullable=False),
        sa.Column("template_revision", sa.String(64), nullable=False),
        sa.Column("profile", sa.String(128), nullable=False),
        sa.Column("name", sa.String(128), nullable=False, server_default="assistant"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("desired_state", sa.String(32), nullable=False),
        sa.Column("observed_state", sa.String(32), nullable=False),
        sa.Column("machine_generation", sa.Integer(), nullable=False),
        sa.Column("auth_readiness", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("id", "user_id", "org_id", "deployment_id", name="uq_dev_boxes_scope"),
        sa.CheckConstraint("machine_generation >= 1", name="ck_dev_boxes_generation"),
    )
    op.create_index(
        "uq_dev_boxes_live_owner",
        "dev_boxes",
        ["user_id", "org_id", "deployment_id"],
        unique=True,
        postgresql_where=sa.text("status != 'released'"),
        sqlite_where=sa.text("status != 'released'"),
    )
    op.create_table(
        "dev_box_instances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("box_id", sa.String(36), nullable=False),
        *scope_columns(),
        sa.Column("provider_instance_id", sa.String(255), unique=True),
        sa.Column("provider_account_ref", sa.String(128)),
        sa.Column("region", sa.String(64)),
        sa.Column("availability_zone", sa.String(64)),
        sa.Column("network_ref", sa.String(255)),
        sa.Column("private_address", sa.String(64)),
        sa.Column("image_ref", sa.String(255)),
        sa.Column("instance_size", sa.String(64)),
        sa.Column("device_identity_id", sa.String(36), sa.ForeignKey("user_identities.id")),
        sa.Column("template_revision", sa.String(64), nullable=False),
        sa.Column("observed_state", sa.String(32), nullable=False),
        sa.Column("readiness_class", sa.String(64)),
        sa.Column("error_class", sa.String(64)),
        scope_fk(),
        sa.UniqueConstraint("box_id", "machine_generation"),
        sa.UniqueConstraint("id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation", name="uq_dev_box_instance_scope"),
    )
    op.create_table(
        "dev_box_resources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("box_id", sa.String(36), nullable=False),
        *scope_columns(),
        sa.Column("resource_kind", sa.String(64), nullable=False),
        sa.Column("provider_resource_id", sa.String(255), nullable=False),
        sa.Column("size_gb", sa.Integer()),
        sa.Column("encrypted", sa.Boolean()),
        sa.Column("retention_state", sa.String(32), nullable=False),
        sa.Column("deletion_state", sa.String(32), nullable=False),
        scope_fk(),
        sa.UniqueConstraint("id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation", name="uq_dev_box_resource_scope"),
    )
    op.create_table(
        "dev_box_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("box_id", sa.String(36), nullable=False),
        *scope_columns(),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("actor_user_id", sa.String(255), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("provider_token", sa.String(64), nullable=False, unique=True),
        sa.Column("provider_request_ref", sa.String(255)),
        sa.Column("progress", sa.String(64), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("error_class", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        scope_fk(),
        sa.UniqueConstraint("user_id", "org_id", "deployment_id", "idempotency_key", name="uq_dev_box_operation_key"),
        sa.UniqueConstraint("id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation", name="uq_dev_box_operation_scope"),
        sa.CheckConstraint("status IN ('pending', 'unknown', 'succeeded', 'failed')", name="ck_dev_box_operation_status"),
    )
    op.create_index(
        "uq_dev_box_active_operation",
        "dev_box_operations",
        ["box_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'unknown')"),
        sqlite_where=sa.text("status IN ('pending', 'unknown')"),
    )
    op.create_table(
        "dev_box_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("box_id", sa.String(36), nullable=False),
        *scope_columns(),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        scope_fk(),
        owned_fk("operation_id", "dev_box_operations"),
    )
    op.create_table(
        "dev_box_usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("box_id", sa.String(36), nullable=False),
        *scope_columns(),
        sa.Column("resource_id", sa.String(36), nullable=False),
        sa.Column("interval_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cost_usd", sa.Numeric(15, 6), nullable=False),
        sa.Column("units", sa.Numeric(15, 6), nullable=False),
        sa.Column("unit_name", sa.String(32), nullable=False),
        sa.Column("rate_usd", sa.Numeric(15, 6), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reconciled", sa.Boolean(), nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        scope_fk(),
        owned_fk("resource_id", "dev_box_resources"),
        sa.UniqueConstraint("resource_id", "interval_start", "interval_end", "source_revision", name="uq_dev_box_usage_interval"),
    )
    op.create_table(
        "dev_box_devices",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("box_id", sa.String(36), nullable=False),
        *scope_columns(),
        sa.Column("instance_id", sa.String(36), nullable=False),
        sa.Column("public_key_digest", sa.String(64), nullable=False),
        sa.Column("enrollment_digest", sa.String(64), unique=True),
        sa.Column("enrollment_expires_at", sa.DateTime(timezone=True)),
        sa.Column("enrollment_consumed_at", sa.DateTime(timezone=True)),
        sa.Column("auth_epoch", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        scope_fk(),
        owned_fk("instance_id", "dev_box_instances"),
        sa.UniqueConstraint("box_id", "machine_generation", "public_key_digest"),
        sa.UniqueConstraint("id", "box_id", "user_id", "org_id", "deployment_id", "machine_generation", name="uq_dev_box_device_scope"),
    )
    op.create_table(
        "dev_box_access_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("box_id", sa.String(36), nullable=False),
        *scope_columns(),
        sa.Column("device_id", sa.String(36), nullable=False),
        sa.Column("provider_session_ref", sa.String(255)),
        sa.Column("certificate_digest", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminated_at", sa.DateTime(timezone=True)),
        scope_fk(),
        owned_fk("device_id", "dev_box_devices"),
    )


def downgrade():
    for table in (
        "dev_box_access_sessions",
        "dev_box_devices",
        "dev_box_usage",
        "dev_box_events",
        "dev_box_operations",
        "dev_box_resources",
        "dev_box_instances",
        "dev_boxes",
        "dev_box_template_versions",
        "dev_box_policies",
    ):
        op.drop_table(table)
