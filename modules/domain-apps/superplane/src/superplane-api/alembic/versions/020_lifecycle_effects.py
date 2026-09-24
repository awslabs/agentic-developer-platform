"""Finite lifecycle SDK effects, append-only intent and confirmed readback.

Revision ID: 020_lifecycle_effects
Revises: 019_lifecycle_artifacts
"""

from alembic import op
import sqlalchemy as sa

revision = "020_lifecycle_effects"
down_revision = "019_lifecycle_artifacts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "workspace_lifecycle_effects",
        sa.Column("org_id", sa.String(255), primary_key=True),
        sa.Column("workspace_id", sa.String(255), primary_key=True),
        sa.Column("operation_id", sa.String(255), primary_key=True),
        sa.Column("phase", sa.String(64), primary_key=True),
        sa.Column("effect_key", sa.String(200), primary_key=True),
        sa.Column("event", sa.String(16), primary_key=True),
        sa.Column("source_job_id", sa.String(255), nullable=False),
        sa.Column("source_payload_digest", sa.String(64), nullable=False),
        sa.Column("recipe_digest", sa.String(64), nullable=False),
        sa.Column("descriptor_digest", sa.String(64), nullable=False),
        sa.Column("descriptor_json", sa.Text(), nullable=False),
        sa.Column("holder", sa.String(255), nullable=False),
        sa.Column("attempt_id", sa.String(255), nullable=False),
        sa.Column("fence_token", sa.BigInteger(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("event_digest", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("event IN ('intended','confirmed')"),
        sa.CheckConstraint("event_digest ~ '^[a-f0-9]{64}$'"),
        sa.CheckConstraint("jsonb_typeof(descriptor_json::jsonb)='object'"),
        sa.CheckConstraint(
            "(event='intended' AND result_json::jsonb='null'::jsonb) OR (event='confirmed' AND jsonb_typeof(result_json::jsonb)='object')"
        ),
    )


def downgrade():
    op.drop_table("workspace_lifecycle_effects")
