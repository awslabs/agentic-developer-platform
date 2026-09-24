"""Join reviewed CLI recovery and bootstrap observation foundations.

Revision ID: 027_cli_bootstrap_foundation
Revises: 021_deployment_identity, 018_bootstrap_read_tokens

Both parent histories remain unchanged. No data or provider mutation occurs.
"""

revision = "027_cli_bootstrap_foundation"
down_revision = ("021_deployment_identity", "018_bootstrap_read_tokens")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
