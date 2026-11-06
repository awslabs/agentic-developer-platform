"""Retain explicit consent continuations without rewriting setup requests."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_setup_authority_continuations"
down_revision = "l5_wa_qualification"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "l5_service_setup_authority_continuations",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("receipt", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("receipt_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id", "operation_id"],
                                ["l5_service_setup_intents.tenant_id", "l5_service_setup_intents.operation_id"]),
        sa.UniqueConstraint("tenant_id", "operation_id", "idempotency_key"),
    )


def downgrade():
    require_empty("l5_service_setup_authority_continuations")
    op.drop_table("l5_service_setup_authority_continuations")
