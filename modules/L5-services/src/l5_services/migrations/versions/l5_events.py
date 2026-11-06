"""Persist semantic event identities and frozen admission inputs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_events"
down_revision = "l5_worker_fences"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "l5_service_event_receipts",
        *[sa.Column(field, sa.String(255), primary_key=True) for field in (
            "tenant_id", "product_id", "project_id", "service_id", "trigger_id", "event_id",
        )],
        sa.Column("event_kind", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("source_ref", payload),
        sa.Column("source_cursor", sa.String(2048)),
        sa.Column("causal_origin", payload),
        sa.Column("configuration_id", sa.String(36), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("target", payload),
        sa.Column("run_input", payload),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("run_id", sa.String(36)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recovery_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_l5_event_admission", "l5_service_event_receipts", ["tenant_id", "idempotency_key"])
    op.create_index("ix_l5_event_retry", "l5_service_event_receipts", ["state", "retry_at", "received_at"])


def downgrade():
    op.drop_index("ix_l5_event_retry", table_name="l5_service_event_receipts")
    op.drop_index("ix_l5_event_admission", table_name="l5_service_event_receipts")
    op.drop_table("l5_service_event_receipts")
