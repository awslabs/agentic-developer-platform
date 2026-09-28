"""Alembic PostgreSQL version storage for maintained descriptive revision IDs.

Several inherited revision IDs exceed Alembic's default32 characters. Use the
public version-table implementation hook (Alembic1.14+) without renaming history.
Only the domain Alembic environment imports this dialect implementation.
"""

from sqlalchemy import String

from alembic.ddl.postgresql import PostgresqlImpl


class SuperplanePostgresqlImpl(PostgresqlImpl):
    __dialect__ = "postgresql"

    def version_table_impl(self, **kwargs):
        table = super().version_table_impl(**kwargs)
        table.c.version_num.type = String(128)
        return table
