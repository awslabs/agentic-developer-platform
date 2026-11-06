"""Install request analytics for databases created exclusively by migrations."""

import sqlalchemy as sa

from alembic import op

revision = "144_request_logs"
down_revision = "143_flow_read_revisions"
branch_labels = None
depends_on = None


def upgrade():
    # Older installations may have created this long-standing model with
    # metadata.create_all. Never replace their table or its existing records.
    if sa.inspect(op.get_bind()).has_table("request_logs"):
        return
    op.create_table(
        "request_logs",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("department_id", sa.String(255)),
        sa.Column("team_id", sa.String(255)),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("path", sa.String(2048), nullable=False),
        sa.Column("query_params", sa.JSON()),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_time_ms", sa.Integer(), nullable=False),
        sa.Column("request_body_size", sa.Integer()),
        sa.Column("response_body_size", sa.Integer()),
        sa.Column("error_message", sa.Text()),
        sa.Column("request_id", sa.String(255)),
        sa.Column("client_ip", sa.String(45)),
        sa.Column("user_agent", sa.String(512)),
        sa.Column("org_id", sa.String(255), nullable=False),
    )
    for column in ("timestamp", "user_id", "department_id", "path", "status_code", "request_id", "org_id"):
        op.create_index(f"ix_request_logs_{column}", "request_logs", [column])


def downgrade():
    # This table predates the migration and is also used by the older synchronous
    # logger. Retain analytics on application/schema rollback, including tables
    # that this migration adopted from a metadata-created installation.
    pass
