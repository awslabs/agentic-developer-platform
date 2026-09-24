"""Record HOW a magic link reached the account it claims — #5664 (A10).

`magic_link_nonces` carried no record of delivery, so the consume path could not
tell a link that was DMed to the claimed account from one posted into a public
channel where anyone could read it. Both wrote the same "proven" identity row.

Left nullable with no server default, and existing rows are NOT backfilled:
`delivery_proves_ownership` treats NULL as unproven, so historical nonces produce
a self-asserted link instead of a trusted one. Choosing any non-null value for
them would assert a private delivery the platform never observed.
"""

import sqlalchemy as sa

from alembic import op

revision = "071_magic_link_delivery_method"
down_revision = "070_opus55_pricing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "magic_link_nonces",
        sa.Column("delivery_method", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("magic_link_nonces", "delivery_method")
