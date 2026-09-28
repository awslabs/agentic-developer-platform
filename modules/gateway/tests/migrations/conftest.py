"""Fixture wiring for the migration tests.

The real-PostgreSQL fixtures live in `conftest_postgres.py` rather than here so
that the helper functions beside them (`run_alembic`, `upgrade`, `downgrade`) can
be imported explicitly by name — a test that shells out to Alembic reads better
when the call is visible than when it arrives as a fixture.

Re-exporting them through this module is what makes them available as fixtures
without each test file importing them and then shadowing the import with a
same-named parameter (which ruff correctly flags as F811). Pytest collects
fixtures from `conftest.py` automatically, so tests just name them.

Issue #4969.
"""

from tests.migrations.conftest_postgres import (
    connect,
    pg_server,
    pg_url,
)

__all__ = ["connect", "pg_server", "pg_url"]
