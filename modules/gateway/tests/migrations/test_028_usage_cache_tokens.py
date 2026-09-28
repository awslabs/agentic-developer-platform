"""Tests for Alembic migration 028 — usage_logs prompt-cache token columns.

Issue #4180: cache-read/cache-write counters make prompt-cache effectiveness
observable. The shape of the columns is the whole point:

  - NULLABLE with NO DEFAULT. ``NULL`` means "the provider did not report this
    counter"; ``0`` means "the provider reported zero cache activity". A default
    of 0 would fuse the two and make the hit-rate query silently wrong in the
    direction that hides the original bug. Nullability is also what keeps the
    usage hot path writable during rollout — a NOT NULL column would fail every
    in-flight INSERT.
  - No index: the reporting query is a windowed SUM aggregate, which an index on
    the summed columns does not serve. Contrast 018's partial index on
    ``agent_run_id``, a point-lookup key.
  - The revision chain: a broken ``down_revision`` silently skips the migration,
    and the gateway then INSERTs columns that do not exist. Because
    ``_log_usage`` swallows exceptions, that failure returns HTTP 200 with no
    usage row at all — unmetered and unbilled, with no alarm.
"""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from src.shared.models.base import Base

CACHE_COLUMNS = ("cache_read_input_tokens", "cache_creation_input_tokens")


def _usage_log_columns(sync_conn):
    insp = sa_inspect(sync_conn)
    return {c["name"]: c for c in insp.get_columns("usage_logs")}


def _usage_log_indexes(sync_conn):
    return sa_inspect(sync_conn).get_indexes("usage_logs")


class TestSchema:
    @pytest.fixture
    async def engine(self):
        engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            echo=False,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield engine
        await engine.dispose()

    @pytest.fixture
    async def columns(self, engine):
        async with engine.connect() as conn:
            return await conn.run_sync(_usage_log_columns)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", CACHE_COLUMNS)
    async def test_column_exists(self, columns, name):
        assert name in columns

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", CACHE_COLUMNS)
    async def test_column_is_nullable(self, columns, name):
        """NULL is a meaningful value here: "provider did not report"."""
        assert columns[name]["nullable"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", CACHE_COLUMNS)
    async def test_column_has_no_default(self, columns, name):
        """A default of 0 would make "unreported" indistinguishable from "no cache use"."""
        assert columns[name].get("default") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", CACHE_COLUMNS)
    async def test_column_is_not_indexed(self, engine, name):
        """SUM aggregates do not benefit from an index on the summed column."""
        async with engine.connect() as conn:
            indexes = await conn.run_sync(_usage_log_indexes)
        indexed = {col for idx in indexes for col in idx["column_names"]}
        assert name not in indexed


class TestRevisionChain:
    @pytest.fixture(scope="class")
    def module(self):
        path = Path(__file__).parents[2] / "alembic" / "versions" / "028_usage_cache_tokens.py"
        spec = importlib.util.spec_from_file_location("m028", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_chains_onto_027(self, module):
        assert module.revision == "028_usage_cache_tokens"
        assert module.down_revision == "027_install_tenant_unique"

    def test_revision_ids_fit_alembic_version_column(self, module):
        """#4123: an id over 32 chars runs upgrade() then rolls back on Postgres.

        SQLite does not enforce VARCHAR length, so CI cannot catch this at
        runtime — only a static check can.
        """
        assert len(module.revision) <= 32
        assert len(module.down_revision) <= 32

    def test_downgrade_is_symmetric(self, module):
        """Present for upgrade/downgrade parity — but see the docstring: it is
        deliberately NOT the incident-response path, because a code revert alone
        restores prior behaviour against these nullable columns."""
        assert callable(module.downgrade)
