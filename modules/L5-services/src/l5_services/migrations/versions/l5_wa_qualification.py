"""Retain observation-only qualification selections without execution authority."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from l5_services.migrations.retained_downgrade import require_empty

revision = "l5_wa_qualification"
down_revision = "l5_wa_reuse_authorization"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "l5_service_wa_qualification",
        sa.Column("tenant_id", sa.String(255), primary_key=True),
        sa.Column("experiment_id", sa.String(36), primary_key=True),
        sa.Column("configuration_id", sa.String(36), nullable=False),
        sa.Column("configuration_revision", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(255), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("selection", sa.JSON().with_variant(JSONB(), "postgresql"), nullable=False),
        sa.Column("selection_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["tenant_id", "configuration_id", "configuration_revision"],
                                ["l5_service_config_versions.tenant_id", "l5_service_config_versions.configuration_id",
                                 "l5_service_config_versions.revision"]),
        sa.CheckConstraint("mode = 'observations'", name="ck_l5_wa_qualification_observations"),
    )
    op.create_index("ix_l5_wa_qualification_owner", "l5_service_wa_qualification", ["tenant_id", "configuration_id", "actor_id"])


def downgrade():
    require_empty("l5_service_wa_qualification")
    op.drop_table("l5_service_wa_qualification")
