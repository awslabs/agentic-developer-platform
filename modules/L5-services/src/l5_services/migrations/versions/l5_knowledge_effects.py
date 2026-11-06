"""Retain fenced knowledge effect history and exact native ownership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_knowledge_effects"
down_revision = "l5_wa_periodic_evidence"
branch_labels = None
depends_on = None


def upgrade():
    payload = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.add_column("l5_service_setup_work", sa.Column("dispatch_attempt_limit", sa.Integer()))
    op.create_table(
        "l5_devops_knowledge_ownership",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("space_id", sa.String(255), primary_key=True),
        sa.Column("source_key", sa.String(64), primary_key=True),
        sa.Column("binding_id", sa.String(36), nullable=False),
        sa.Column("operation_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("receipt", payload),
    )
    op.create_table(
        "l5_devops_knowledge_effects",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("operation_id", sa.String(36), primary_key=True),
        sa.Column("source_key", sa.String(64), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("work_id", sa.String(36), nullable=False),
        sa.Column("lease_owner", sa.String(255), nullable=False),
        sa.Column("fence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", payload, nullable=False),
        sa.Column("previous_hash", sa.String(64)),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            [
                "l5_service_setup_intents.tenant_id",
                "l5_service_setup_intents.operation_id",
            ],
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE FUNCTION l5_knowledge_effect_immutable() RETURNS trigger LANGUAGE plpgsql "
            "AS $$ BEGIN RAISE EXCEPTION 'Knowledge effects are append-only'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER l5_knowledge_effect_immutable BEFORE UPDATE OR DELETE ON l5_devops_knowledge_effects "
            "FOR EACH ROW EXECUTE FUNCTION l5_knowledge_effect_immutable()"
        )


def downgrade():
    require_empty("l5_service_setup_work", "dispatch_attempt_limit")
    for table in ("l5_devops_knowledge_effects", "l5_devops_knowledge_ownership"):
        require_empty(table)
    op.drop_table("l5_devops_knowledge_effects")
    op.drop_table("l5_devops_knowledge_ownership")
    op.drop_column("l5_service_setup_work", "dispatch_attempt_limit")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION l5_knowledge_effect_immutable()")
