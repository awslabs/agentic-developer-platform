"""Merge shared Jira context and add explicit Forge installation authority."""

import sqlalchemy as sa

from alembic import op

revision = "181_atlassian_forge"
down_revision = ("169_atlassian_system", "180_l5_knowledge_evidence")
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("atlassian_system_connections", sa.Column("connection_method", sa.String(32), nullable=False, server_default="service_account"))
    for name, size in (
        ("forge_app_id", 255),
        ("forge_environment_id", 255),
        ("forge_installation_id", 255),
        ("pairing_hash", 64),
        ("forge_token_hash", 64),
    ):
        op.add_column("atlassian_system_connections", sa.Column(name, sa.String(size)))
    op.add_column("atlassian_system_connections", sa.Column("pairing_expires_at", sa.DateTime(timezone=True)))
    op.create_index("ix_atlassian_system_pairing", "atlassian_system_connections", ["pairing_hash"], unique=True)
    op.create_index(
        "ix_atlassian_system_installation",
        "atlassian_system_connections",
        ["forge_app_id", "forge_environment_id", "forge_installation_id"],
        unique=True,
    )


def downgrade():
    raise RuntimeError("Retain Jira Identity provider references during application rollback; revoke connections through the managed API.")
