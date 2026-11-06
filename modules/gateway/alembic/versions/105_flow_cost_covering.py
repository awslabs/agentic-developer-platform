"""Cover tenant-scoped flow cost reads without scanning the usage ledger heap."""

import sqlalchemy as sa

from alembic import op

revision = "105_flow_cost_covering"
down_revision = "104_decision_display_projection"
branch_labels = None
depends_on = None

INDEX = "ix_usage_flow_cost_covering"


def _index(*, drop):
    if op.get_bind().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        # Concurrent builds wait for writers and old snapshots without blocking
        # ordinary reads/writes. Normal transactions routinely exceed one second.
        op.execute("SET lock_timeout = '30s'")
        op.execute("SET statement_timeout = '5min'")
        try:
            valid = op.get_bind().scalar(sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"), {"name": INDEX})
            if drop or valid is False:
                op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{INDEX}"')
            if not drop and valid is not True:
                op.execute(f'''CREATE INDEX CONCURRENTLY "{INDEX}" ON usage_logs
                    (org_id, graph_address varchar_pattern_ops)
                    INCLUDE (cost_usd, input_tokens, output_tokens, id)
                    WHERE graph_address IS NOT NULL''')
        finally:
            op.execute("RESET lock_timeout")
            op.execute("RESET statement_timeout")


def upgrade():
    _index(drop=False)


def downgrade():
    _index(drop=True)
