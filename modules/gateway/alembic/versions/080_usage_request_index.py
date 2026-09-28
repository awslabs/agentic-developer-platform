"""Index scoped request lookups for settlement and historical credits.

A failed concurrent build may leave an invalid index. Retrying this revision
removes only that invalid index before rebuilding; completed indexes survive.
"""

import sqlalchemy as sa

from alembic import op

revision = "080_usage_request_index"
down_revision = "079_budget_pricing_corrections"
branch_labels = None
depends_on = None

INDEXES = {"ix_usage_org_request": (["org_id", "request_id"], "request_id IS NOT NULL")}


def _apply(*, drop: bool) -> None:
    postgres = op.get_bind().dialect.name == "postgresql"
    if not postgres:
        for name, (columns, predicate) in INDEXES.items():
            if drop:
                op.drop_index(name, table_name="usage_logs")
            else:
                op.create_index(name, "usage_logs", columns, sqlite_where=sa.text(predicate))
        return
    with op.get_context().autocommit_block():
        op.execute("SET lock_timeout = '1s'")
        op.execute("SET statement_timeout = '5min'")
        try:
            for name, (columns, predicate) in INDEXES.items():
                valid = op.get_bind().scalar(sa.text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"), {"name": name})
                if drop or valid is False:
                    op.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')
                if not drop and valid is not True:
                    op.create_index(name, "usage_logs", columns, postgresql_where=sa.text(predicate), postgresql_concurrently=True)
        finally:
            op.execute("RESET lock_timeout")
            op.execute("RESET statement_timeout")


def upgrade() -> None:
    _apply(drop=False)


def downgrade() -> None:
    _apply(drop=True)
