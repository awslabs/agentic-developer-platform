"""Join workspace bootstrap and CLI lifecycle migrations without rewriting either.

Revision ID: 020_merge_workspace_cli
Revises: 019_workspace_operation_state, 017_add_workspace_bootstrap_reservations
"""

revision = "020_merge_workspace_cli"
down_revision = (
    "019_workspace_operation_state",
    "017_add_workspace_bootstrap_reservations",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
