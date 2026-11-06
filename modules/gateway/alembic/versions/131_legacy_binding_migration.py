"""Keep audited legacy attribution and amendment receipts without editing plans."""

import sqlalchemy as sa

from alembic import op

revision = "131_legacy_binding_migration"
down_revision = "130_shared_publication_intent"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "orchestration_legacy_migrations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("org_id", sa.String(36), nullable=False),
        sa.Column("flow_id", sa.String(36), sa.ForeignKey("orchestration_flows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("product_id", sa.String(36), nullable=True),
        sa.Column("node_id", sa.String(36), sa.ForeignKey("orchestration_nodes.id"), nullable=True),
        sa.Column("base_plan_version", sa.Integer(), nullable=False),
        sa.Column("base_plan_hash", sa.String(64), nullable=False),
        sa.Column("result_plan_version", sa.Integer(), nullable=False),
        sa.Column("decision_id", sa.String(36), sa.ForeignKey("orchestration_decisions.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_orchestration_legacy_migrations_request", "orchestration_legacy_migrations", ["org_id", "flow_id", "request_id"], unique=True)


def downgrade():
    raise RuntimeError("Retain legacy migration decisions and expanded schema during rollback")
