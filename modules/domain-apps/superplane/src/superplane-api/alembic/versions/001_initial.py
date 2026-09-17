"""Initial schema — all tables from design doc sections 6.1 and 15.7.

Revision ID: 001_initial
Revises:
Create Date: 2026-03-30
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- organizations ---
    op.create_table(
        "organizations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), unique=True, nullable=False),
        sa.Column("billing_plan", sa.String(50), nullable=False, server_default="free"),
        sa.Column("quotas_json", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    # --- cloud_accounts (section 15.7) ---
    op.create_table(
        "cloud_accounts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("account_identifier", sa.String(255), nullable=False),
        sa.Column("friendly_name", sa.String(255), nullable=False),
        sa.Column("provisioning_mode", sa.String(50), nullable=False),
        sa.Column("cross_account_role_arn", sa.String(512), nullable=True),
        sa.Column("cfn_stack_id", sa.String(512), nullable=True),
        sa.Column(
            "status", sa.String(50), nullable=False, server_default="Provisioning"
        ),
        sa.Column("metadata_json", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    # --- clusters (section 6.1) ---
    op.create_table(
        "clusters",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("workspace_id", UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("desired_state_json", sa.Text, nullable=True),
        sa.Column("actual_state_json", sa.Text, nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="Pending"),
        sa.Column("cloud_provider", sa.String(50), nullable=True),
        sa.Column("cluster_type", sa.String(50), nullable=True),
        sa.Column("eks_cluster_arn", sa.String(512), nullable=True),
        sa.Column("hyperpod_cluster_arn", sa.String(512), nullable=True),
        sa.Column("cfn_stack_id", sa.String(512), nullable=True),
        sa.Column("endpoint", sa.String(512), nullable=True),
        sa.Column("health_status", sa.String(50), nullable=True),
        sa.Column("last_heartbeat", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    # --- workspaces (section 15.7 — enriched) ---
    op.create_table(
        "workspaces",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("isolation_mode", sa.String(50), nullable=False),
        sa.Column(
            "aws_account_id",
            UUID(as_uuid=True),
            sa.ForeignKey("cloud_accounts.id"),
            nullable=True,
        ),
        sa.Column(
            "cluster_id",
            UUID(as_uuid=True),
            sa.ForeignKey("clusters.id"),
            nullable=True,
        ),
        sa.Column(
            "shared_cluster_id",
            UUID(as_uuid=True),
            sa.ForeignKey("clusters.id"),
            nullable=True,
        ),
        sa.Column("namespace_name", sa.String(255), nullable=True),
        sa.Column("quotas_json", sa.Text, nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="pending"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    # --- node_pools (section 6.1) ---
    op.create_table(
        "node_pools",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "cluster_id",
            UUID(as_uuid=True),
            sa.ForeignKey("clusters.id"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("desired_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("actual_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cloud", sa.String(50), nullable=True),
        sa.Column("gpu_type", sa.String(50), nullable=True),
        sa.Column("gpu_count_per_node", sa.Integer, nullable=True),
        sa.Column("instance_type", sa.String(100), nullable=True),
        sa.Column("disk_size_gb", sa.Integer, nullable=True),
        sa.Column("autoscale_min", sa.Integer, nullable=True),
        sa.Column("autoscale_max", sa.Integer, nullable=True),
        sa.Column("spot_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("status", sa.String(50), nullable=False, server_default="Pending"),
        sa.Column("reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    # --- nodes (section 6.1) ---
    op.create_table(
        "nodes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "node_pool_id",
            UUID(as_uuid=True),
            sa.ForeignKey("node_pools.id"),
            nullable=False,
        ),
        sa.Column(
            "cluster_id",
            UUID(as_uuid=True),
            sa.ForeignKey("clusters.id"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("cloud", sa.String(50), nullable=True),
        sa.Column("region", sa.String(50), nullable=True),
        sa.Column("instance_id", sa.String(255), nullable=True),
        sa.Column("skypilot_cluster_name", sa.String(255), nullable=True),
        sa.Column("k8s_node_name", sa.String(255), nullable=True),
        sa.Column("ssm_instance_id", sa.String(255), nullable=True),
        sa.Column("public_ip", sa.String(45), nullable=True),
        sa.Column("private_ip", sa.String(45), nullable=True),
        sa.Column("gpu_type", sa.String(50), nullable=True),
        sa.Column("gpu_count", sa.Integer, nullable=True),
        sa.Column("gpu_memory_gib", sa.Integer, nullable=True),
        sa.Column(
            "status", sa.String(50), nullable=False, server_default="Provisioning"
        ),
        sa.Column("health_status", sa.String(50), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hourly_cost_usd", sa.Numeric(10, 4), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("terminated_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- deployments (section 6.1) ---
    op.create_table(
        "deployments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "cluster_id",
            UUID(as_uuid=True),
            sa.ForeignKey("clusters.id"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_id",
            UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id"),
            nullable=True,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("model_name", sa.String(255), nullable=True),
        sa.Column("model_revision", sa.String(100), nullable=True),
        sa.Column("precision", sa.String(20), nullable=True),
        sa.Column("serving_framework", sa.String(50), nullable=True),
        sa.Column("desired_replicas", sa.Integer, nullable=False, server_default="1"),
        sa.Column("actual_replicas", sa.Integer, nullable=False, server_default="0"),
        sa.Column("gpu_per_replica", sa.Integer, nullable=True),
        sa.Column("tensor_parallel_size", sa.Integer, nullable=True),
        sa.Column("max_model_len", sa.Integer, nullable=True),
        sa.Column("endpoint_url", sa.String(512), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="Pending"),
        sa.Column("reconcile_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    # --- events (section 6.1) ---
    op.create_table(
        "events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_id", UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("details_json", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    # --- credential_registry (section 15.7) ---
    op.create_table(
        "credential_registry",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("friendly_name", sa.String(255), nullable=False),
        sa.Column("credential_type", sa.String(50), nullable=False),
        sa.Column("secret_arn", sa.String(512), nullable=False),
        sa.Column("kms_key_id", sa.String(512), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="Active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    # --- cluster_vault_assignments (section 15.7) ---
    op.create_table(
        "cluster_vault_assignments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "cluster_id",
            UUID(as_uuid=True),
            sa.ForeignKey("clusters.id"),
            nullable=False,
        ),
        sa.Column(
            "credential_registry_id",
            UUID(as_uuid=True),
            sa.ForeignKey("credential_registry.id"),
            nullable=False,
        ),
        sa.Column("assigned_by", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "assigned_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="Pending"),
    )

    # --- credential_audit_log (section 15.7) ---
    op.create_table(
        "credential_audit_log",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "credential_registry_id",
            UUID(as_uuid=True),
            sa.ForeignKey("credential_registry.id"),
            nullable=False,
        ),
        sa.Column(
            "cluster_id",
            UUID(as_uuid=True),
            sa.ForeignKey("clusters.id"),
            nullable=True,
        ),
        sa.Column("accessed_by", sa.String(255), nullable=True),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("source_ip", sa.String(45), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
    )

    # --- reconcile_locks (section 6.1) ---
    op.create_table(
        "reconcile_locks",
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("locked_by", sa.String(255), nullable=False),
        sa.Column(
            "locked_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("resource_type", "resource_id"),
    )


def downgrade() -> None:
    op.drop_table("reconcile_locks")
    op.drop_table("credential_audit_log")
    op.drop_table("cluster_vault_assignments")
    op.drop_table("credential_registry")
    op.drop_table("events")
    op.drop_table("deployments")
    op.drop_table("nodes")
    op.drop_table("node_pools")
    op.drop_table("workspaces")
    op.drop_table("clusters")
    op.drop_table("cloud_accounts")
    op.drop_table("organizations")
