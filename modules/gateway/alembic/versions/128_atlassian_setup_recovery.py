"""Retain Atlassian deployment setup intent and recovery jobs.

Revision ID: 128_atlassian_setup_recovery
Revises: 127_authoring_assignment
"""

import sqlalchemy as sa

from alembic import op

revision = "128_atlassian_setup_recovery"
down_revision = "127_authoring_assignment"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "oauth_setup_states",
        sa.Column("deployment_key", sa.String(128), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("registration_mode", sa.String(64), nullable=False),
        sa.Column("config_digest", sa.String(64), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("provider_ref", sa.String(255)),
        sa.Column("client_ref", sa.String(255)),
        sa.Column("policy_result", sa.String(32)),
        sa.Column("policy_evidence_ref", sa.String(64)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "oauth_recovery_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("setup_key", sa.String(128), sa.ForeignKey("oauth_setup_states.deployment_key"), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("owner_ref", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("expected_revision", sa.String(64), nullable=False),
        sa.Column("evidence_ref", sa.String(255)),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error_category", sa.String(64)),
        sa.Column("terminal_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("setup_key", "kind", "expected_revision", name="uq_oauth_recovery_intent"),
    )
    op.create_index("ix_oauth_recovery_due", "oauth_recovery_jobs", ["state", "due_at"])
    op.create_table(
        "oauth_tenant_policy_observations",
        sa.Column("setup_key", sa.String(128), sa.ForeignKey("oauth_setup_states.deployment_key"), primary_key=True),
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("config_digest", sa.String(64), nullable=False),
        sa.Column("policy_result", sa.String(32), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    raise RuntimeError("Atlassian setup and unresolved recovery evidence must survive rollback")
