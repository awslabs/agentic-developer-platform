"""Add api_keys table — hashed API keys for programmatic authentication.

Revision ID: 008_add_api_keys_table
Revises: 007_add_users_table
Create Date: 2026-09-17

WHY THIS MIGRATION EXISTS (issue #5045, U13)

`app/models/api_key.py` declares the `api_keys` table and the application queries it,
but no migration in this chain ever created it. A fresh database therefore reached the
chain's head with this table absent — a gap a head-count check cannot see, which is why
tests/test_migrations.py now asserts model/migration table parity directly.

The column set mirrors `app/models/api_key.py` exactly. `key_hash` carries the unique
constraint the model declares (`unique=True`) because lookups authenticate by hashed key,
so a duplicate would make the owning organization of a key ambiguous. Only the HASH is
stored; the plaintext key is never persisted.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "008_add_api_keys_table"
down_revision = "007_add_users_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("key_hash", sa.String(512), nullable=False, unique=True),
        # Displayed to the user so a key can be identified in the UI without revealing it.
        # Width is 16, not the model's original 12: `app/routers/auth.py` stores
        # `raw_key[:12] + "..."`, which is 15 characters. At VARCHAR(12) PostgreSQL
        # rejects that insert with 22001 StringDataRightTruncation, so creating an API
        # key would fail against a real database. The existing tests do not catch it
        # because they run on SQLite, which does not enforce VARCHAR limits.
        sa.Column("key_prefix", sa.String(16), nullable=False),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Authentication resolves a presented key by its hash, so this index is on the hot path.
    op.create_index("ix_api_keys_key_hash", "api_keys", ["key_hash"], unique=True)
    # Listing an organization's keys is the other access pattern.
    op.create_index("ix_api_keys_org_id", "api_keys", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_api_keys_org_id", table_name="api_keys")
    op.drop_index("ix_api_keys_key_hash", table_name="api_keys")
    op.drop_table("api_keys")
