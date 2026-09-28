"""Run the actual normalization against stored configs, including conflicting scopes."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.shared.models.usage import RateLimitConfig

path = Path(__file__).resolve().parents[2] / "alembic/versions/044_ratelimit_org_entity_type.py"
spec = importlib.util.spec_from_file_location("migration_044", path)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def run_migration(connection, function):
    with Operations.context(MigrationContext.configure(connection)):
        function()


async def test_normalizes_without_changing_values_and_is_repeatable(test_engine):
    table = RateLimitConfig.__table__
    rows = [
        {"id": "old", "org_id": "acme", "entity_type": "organization", "entity_id": "acme", "rpm": 2},
        {"id": "new", "org_id": "other", "entity_type": "org", "entity_id": "other", "rpm": 3},
        {"id": "team", "org_id": "acme", "entity_type": "team", "entity_id": "platform", "rpm": 4},
    ]
    async with test_engine.begin() as connection:
        await connection.execute(table.insert(), rows)
        await connection.run_sync(run_migration, migration.upgrade)
        await connection.run_sync(run_migration, migration.upgrade)
        await connection.run_sync(run_migration, migration.downgrade)
        result = (await connection.execute(sa.select(table.c.id, table.c.entity_type, table.c.rpm).order_by(table.c.id))).all()
    assert result == [("new", "org", 3), ("old", "org", 2), ("team", "team", 4)]


async def test_duplicate_scope_aborts_before_changing_any_row(test_engine):
    table = RateLimitConfig.__table__
    async with test_engine.begin() as connection:
        await connection.execute(
            table.insert(),
            [
                {"id": "old", "org_id": "acme", "entity_type": "organization", "entity_id": "acme", "rpm": 2},
                {"id": "new", "org_id": "acme", "entity_type": "org", "entity_id": "acme", "rpm": 3},
            ],
        )
        with pytest.raises(RuntimeError, match="Duplicate organization rate limits require review"):
            await connection.run_sync(run_migration, migration.upgrade)
        result = (await connection.execute(sa.select(table.c.id, table.c.entity_type).order_by(table.c.id))).all()
    assert result == [("new", "org"), ("old", "organization")]
