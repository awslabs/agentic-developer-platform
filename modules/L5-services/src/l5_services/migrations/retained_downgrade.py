"""Refuse schema downgrades that would discard retained provider work."""

import sqlalchemy as sa
from alembic import op


def require_empty(table_name, column_name=None):
    table = sa.table(table_name, sa.column(column_name or "tenant_id"))
    statement = sa.select(sa.literal(1)).select_from(table).limit(1)
    if column_name is not None:
        statement = statement.where(table.c[column_name].is_not(None))
    if op.get_bind().execute(statement).first() is not None:
        raise RuntimeError(f"downgrade would discard retained data in {table_name}")
