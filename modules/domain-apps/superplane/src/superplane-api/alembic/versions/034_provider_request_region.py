"""Record which approved region a launch actually used, as soon as it is known.

Multi-region plans (#5925) approve a bounded set of regions; SkyPilot -- not this
app -- picks one. The provider journal previously had no place to persist that
choice, so a lost launch reply left recovery re-deriving the region by re-checking
every approved region every time. This column is filled in as soon as the launch
is observed (see `Provider.remember`), independently of the existing SkyPilot
request-handle column it sits beside.
"""

import sqlalchemy as sa
from alembic import op

revision = "034_provider_request_region"
down_revision = "033_retained_batch_results"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "controller_provider_requests",
        sa.Column("region", sa.String(32), nullable=True),
    )


def downgrade():
    op.drop_column("controller_provider_requests", "region")
