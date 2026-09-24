"""Join reviewed API, bootstrap observation and controller journals additively."""

revision = "025_merge_governed_runtime"
down_revision = (
    "024_approval_creation_scope",
    "018_bootstrap_read_tokens",
    "018_controller_executions",
)
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
