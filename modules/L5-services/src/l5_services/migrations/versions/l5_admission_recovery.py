"""Persist capture preparation, mutation receipts and retry evidence."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "l5_admission_recovery"
down_revision = "l5_runs_and_work"
branch_labels = None
depends_on = None

payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "l5_service_admission_preparations",
        *[sa.Column(field, sa.String(32 if field == "operation" else 255), primary_key=True)
          for field in ("tenant_id", "product_id", "project_id", "service_id", "actor_id",
                        "operation", "idempotency_key")],
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("verified", payload, nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column("l5_service_receipts", sa.Column("request", payload))
    op.add_column("l5_service_receipts", sa.Column("response", payload))
    op.add_column("l5_service_runs", sa.Column("no_effect_proof", payload))
    op.add_column("l5_service_runs", sa.Column("retry_of", sa.String(36)))


def downgrade():
    op.drop_column("l5_service_runs", "retry_of")
    op.drop_column("l5_service_runs", "no_effect_proof")
    op.drop_column("l5_service_receipts", "response")
    op.drop_column("l5_service_receipts", "request")
    op.drop_table("l5_service_admission_preparations")
