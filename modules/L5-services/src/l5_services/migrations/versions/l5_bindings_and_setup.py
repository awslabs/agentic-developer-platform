"""Persist Security Agent binding and immutable setup intent.

Revision ID: l5_bindings_and_setup
Revises: l5_devops_resource_observation
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_bindings_and_setup"
down_revision = "l5_devops_resource_observation"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "l5_service_bindings",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("binding_id", sa.String(36), primary_key=True),
        sa.Column("configuration_id", sa.String(36), nullable=False),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("service_id", sa.String(32), nullable=False),
        sa.Column("scope", payload, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("ownership", sa.String(32), nullable=False),
        sa.Column("connection_ref", payload, nullable=False),
        sa.Column("connection_key", sa.String(64), nullable=False),
        sa.Column("native_resource_ref", payload),
        sa.Column("native_key", sa.String(64)),
        sa.Column("provider_identity", payload),
        sa.Column("creation_receipt", payload),
        sa.Column("readiness", payload, nullable=False),
        sa.Column("gaps", payload, nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "configuration_id"], ["l5_service_configurations.tenant_id", "l5_service_configurations.configuration_id"]),
        sa.UniqueConstraint("tenant_id", "configuration_id", "connection_key", "native_key"),
    )
    op.create_table(
        "l5_service_setup_intents",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("product_id", sa.String(255), nullable=False),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("service_id", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("configuration_id", sa.String(36), nullable=False),
        sa.Column("scope", payload, nullable=False),
        sa.Column("request", payload, nullable=False),
        sa.Column("binding_generation", sa.Integer()),
        sa.Column("authorization", payload, nullable=False),
        sa.Column("owned_resource_receipt", payload),
        sa.Column("intent_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "configuration_id"], ["l5_service_configurations.tenant_id", "l5_service_configurations.configuration_id"]),
        sa.UniqueConstraint("tenant_id", "product_id", "project_id", "service_id", "actor_id", "idempotency_key"),
    )
    op.create_table(
        "l5_service_binding_operations",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("binding_id", sa.String(36)),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("gaps", payload, nullable=False),
        sa.Column("readback_ref", payload),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reconcile_checks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("native_resource_ref", payload),
        sa.Column("creation_receipt", payload),
        sa.Column("native_candidate_id", sa.String(255)),
        sa.Column("reconcile_key", sa.String(128)),
        sa.Column("reconcile_reason", sa.String(1000)),
        sa.ForeignKeyConstraint(["tenant_id", "operation_id"], ["l5_service_setup_intents.tenant_id", "l5_service_setup_intents.operation_id"]),
    )
    op.create_table(
        "l5_service_setup_work",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("work_id", sa.String(36), primary_key=True),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_at", sa.DateTime(timezone=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("dispatch_owner", sa.String(255)),
        sa.Column("dispatch_until", sa.DateTime(timezone=True)),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True)),
        sa.Column("redeliver_after", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(1024)),
        sa.ForeignKeyConstraint(["tenant_id", "operation_id"], ["l5_service_setup_intents.tenant_id", "l5_service_setup_intents.operation_id"]),
        sa.UniqueConstraint("tenant_id", "operation_id", "generation"),
    )


def downgrade():
    op.drop_table("l5_service_setup_work")
    op.drop_table("l5_service_binding_operations")
    op.drop_table("l5_service_setup_intents")
    op.drop_table("l5_service_bindings")
