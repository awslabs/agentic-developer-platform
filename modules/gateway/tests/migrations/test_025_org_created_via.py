"""Tests for Alembic migration 025 — organizations.created_via provenance column.

Issue #2724 (slice B): the webhook auto-register gate needs to distinguish a
tenant an operator or an authenticated ADP flow onboarded from a shell the
platform auto-created for whoever clicked Install on the public App. Tenant
*existence* cannot carry that distinction — the unauthenticated no-nonce install
callback creates the row itself, so existence is a signal the installer
manufactured. ``created_via`` carries it instead.

Verifies:
  - The column exists, is non-nullable, and is width-bounded
  - ``server_default='operator'`` IS the backfill: existing rows (and any INSERT
    that omits the column, e.g. a raw SQL insert from an older code path)
    grandfather in as trusted, so this change cannot evict live tenants
  - The ORM default matches the server default, so both writers agree
  - All three provenance values round-trip
  - The revision chains onto 024 (a broken chain silently skips the migration,
    and the gate then reads a column that does not exist)
"""

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.shared.models.base import Base
from src.shared.models.organization import Organization


def _org_columns(sync_conn):
    insp = sa_inspect(sync_conn)
    return {c["name"]: c for c in insp.get_columns("organizations")}


async def _create_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


class TestSchema:
    @pytest.fixture
    async def columns(self):
        engine = await _create_engine()
        async with engine.connect() as conn:
            cols = await conn.run_sync(_org_columns)
        await engine.dispose()
        return cols

    @pytest.mark.asyncio
    async def test_column_exists(self, columns):
        assert "created_via" in columns

    @pytest.mark.asyncio
    async def test_column_is_not_nullable(self, columns):
        """Non-nullable so there is no third "unknown" state in the database.

        A NULL would have to be interpreted at every read site, and the safe
        interpretation differs by site — exactly the ambiguity this column
        exists to remove.
        """
        assert columns["created_via"]["nullable"] is False

    @pytest.mark.asyncio
    async def test_column_has_server_default_operator(self, columns):
        """The server default IS the backfill for existing deployments."""
        default = columns["created_via"].get("default") or ""
        assert "operator" in default

    @pytest.mark.asyncio
    async def test_column_is_width_bounded(self, columns):
        """A bounded VARCHAR, matching the model — this is an enum-ish tag, not free text."""
        assert columns["created_via"]["type"].length == 32


class TestGrandfathering:
    @pytest.fixture
    async def engine(self):
        engine = await _create_engine()
        yield engine
        await engine.dispose()

    @pytest.mark.asyncio
    async def test_raw_insert_without_column_defaults_to_operator(self, engine):
        """A row written without the column reads back as operator, not empty.

        This is what makes the migration a backfill rather than a breaking
        change: every organization that existed before this column is trusted
        by the gate, so tightening auto-register cannot deny tenants that are
        already live.
        """
        # Build the INSERT from the reflected schema so the test does not rot as
        # columns are added to organizations: supply every NOT NULL column that
        # has no default, and deliberately omit created_via so the server default
        # is the only thing that can fill it.
        placeholders = {
            "String": "'x'",
            "JSON": "'{}'",
            "DateTime": "'2026-01-01 00:00:00'",
            "Boolean": "0",
            "Integer": "0",
        }

        def _required_columns(sync_conn):
            insp = sa_inspect(sync_conn)
            required = []
            for col in insp.get_columns("organizations"):
                if col["name"] == "created_via":
                    continue
                if col["nullable"] or col.get("default") is not None:
                    continue
                required.append((col["name"], type(col["type"]).__name__))
            return required

        async with engine.connect() as conn:
            required = await conn.run_sync(_required_columns)

        names = ["id", "name"] + [n for n, _ in required if n not in ("id", "name")]
        by_name = dict(required)
        values = []
        for name in names:
            if name == "id":
                values.append("'legacy-org'")
            elif name == "name":
                values.append("'Legacy'")
            else:
                values.append(placeholders.get(by_name[name], "'x'"))

        async with engine.begin() as conn:
            await conn.execute(sa.text(f"INSERT INTO organizations ({', '.join(names)}) VALUES ({', '.join(values)})"))
        async with engine.connect() as conn:
            value = (await conn.execute(sa.text("SELECT created_via FROM organizations WHERE id = 'legacy-org'"))).scalar_one()
        assert value == "operator"

    @pytest.mark.asyncio
    async def test_orm_default_matches_server_default(self, engine):
        """Both writers must agree, or provenance depends on which one ran."""
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as session:
            session.add(Organization(id="orm-org", name="ORM Org"))
            await session.commit()
            org = await session.get(Organization, "orm-org")
            assert org.created_via == "operator"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["operator", "register_flow", "install_autocreate"])
    async def test_all_provenance_values_round_trip(self, engine, value):
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as session:
            session.add(Organization(id=f"org-{value}", name=value, created_via=value))
            await session.commit()
            org = await session.get(Organization, f"org-{value}")
            assert org.created_via == value


class TestRevisionChain:
    def test_chains_onto_024(self):
        """A broken down_revision silently skips the migration on deploy.

        The failure mode is nasty: the gateway starts, the gate queries a column
        that does not exist, resolve-installation 500s, and the webhook's
        fail-open path allows everything — the vulnerability, but now with a
        metric claiming the gate is degraded.
        """
        import importlib.util
        from pathlib import Path

        path = Path(__file__).parent.parent.parent / "alembic" / "versions" / "025_org_created_via.py"
        spec = importlib.util.spec_from_file_location("m025", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.revision == "025_org_created_via"
        assert module.down_revision == "024_budget_usage_bigint_tokens"
