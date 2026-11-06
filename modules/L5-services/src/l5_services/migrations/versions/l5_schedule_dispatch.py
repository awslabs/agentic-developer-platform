"""Retain shared scheduler cursors and event arrival/decision history."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_schedule_dispatch"
down_revision = "l5_setup_authority_continuations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "l5_service_schedule_states",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("configuration_id", sa.String(36), primary_key=True),
        sa.Column("configuration_revision", sa.Integer(), primary_key=True),
        sa.Column("service_id", sa.String(255), nullable=False),
        sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_due_at", sa.DateTime(timezone=True)),
        sa.Column("triggers", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
    )
    op.create_index("ix_l5_schedule_due", "l5_service_schedule_states", ["service_id", "next_due_at"])
    op.create_table(
        "l5_service_event_observations",
        sa.Column("observation_id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("run_id", sa.String(36)),
    )
    op.create_index("ix_l5_event_observed", "l5_service_event_observations", ["tenant_id", "idempotency_key", "observed_at"])


def downgrade():
    require_empty("l5_service_event_observations")
    require_empty("l5_service_schedule_states")
    op.drop_table("l5_service_event_observations")
    op.drop_table("l5_service_schedule_states")
