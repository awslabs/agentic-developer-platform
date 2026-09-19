"""Build PMM usage indexes concurrently, separately from the bounded column DDL.

A failed concurrent build may leave an invalid index. Retrying this revision
removes only that invalid index before rebuilding; completed indexes survive.
"""

import sqlalchemy as sa

from alembic import op

revision = "062_persona_usage_indexes"
down_revision = "061_persona_usage_evidence"
branch_labels = None
depends_on = None

INDEXES = {
    "ix_usage_persona_owner": (
        ["org_id", "preference_owner_kind", "preference_owner_id", "persona_key"],
        "persona_key IS NOT NULL AND preference_owner_id IS NOT NULL",
    ),
    "ix_usage_chain_id": (["org_id", "chain_id"], "chain_id IS NOT NULL"),
}


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
