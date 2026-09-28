"""Add platform persona defaults without activating unproven model choices."""

import sqlalchemy as sa

from alembic import op

revision = "081_persona_platform_defaults"
down_revision = "080_usage_request_index"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "persona_platform_defaults",
        sa.Column("persona_key", sa.String(128), primary_key=True),
        sa.Column("compatibility_class", sa.String(64), nullable=False),
        sa.Column("canonical_model_id", sa.String(255), nullable=True),
        sa.Column("harness_contract_revision", sa.String(64), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 1", name="ck_persona_platform_default_revision"),
    )


def downgrade():
    op.drop_table("persona_platform_defaults")
