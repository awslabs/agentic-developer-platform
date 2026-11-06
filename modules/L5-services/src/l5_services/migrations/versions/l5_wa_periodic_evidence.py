"""Retain actual WA transport and periodic checkpoint evidence; never backfill."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_wa_periodic_evidence"
down_revision = "l5_schedule_dispatch"
branch_labels = None
depends_on = None


def upgrade():
    payload = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table("l5_wa_transport_observations",
        sa.Column("observation_id", sa.String(36), primary_key=True),
        sa.Column("send_id", sa.String(36), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("configuration_id", sa.String(36), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("attempt_id", sa.String(36), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("facts", payload, nullable=False))
    op.create_index("ix_l5_wa_transport_scope", "l5_wa_transport_observations",
                    ["tenant_id", "configuration_id", "observed_at"])
    op.create_table("l5_wa_periodic_checkpoints",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("checkpoint_id", sa.String(36), primary_key=True),
        sa.Column("configuration_id", sa.String(36), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("snapshot_digest", sa.String(64), nullable=False),
        sa.Column("snapshot", payload, nullable=False))
    op.create_index("ix_l5_wa_periodic_owner", "l5_wa_periodic_checkpoints",
                    ["tenant_id", "configuration_id", "actor_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE FUNCTION l5_wa_periodic_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'WA periodic evidence is append-only'; END; $$")
        for table in ("l5_wa_transport_observations", "l5_wa_periodic_checkpoints"):
            op.execute(f"CREATE TRIGGER l5_wa_periodic_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION l5_wa_periodic_immutable()")


def downgrade():
    for table in ("l5_wa_periodic_checkpoints", "l5_wa_transport_observations"):
        require_empty(table)
    for table in ("l5_wa_periodic_checkpoints", "l5_wa_transport_observations"):
        op.drop_table(table)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION l5_wa_periodic_immutable()")
