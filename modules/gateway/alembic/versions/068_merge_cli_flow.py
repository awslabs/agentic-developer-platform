"""Join CLI credential recovery and flow pause migrations without rewriting either."""

revision = "068_merge_cli_flow"
down_revision = ("067_aws_connection_verify", "067_flow_execution_pause")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
