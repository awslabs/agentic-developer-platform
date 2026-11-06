"""Real PostgreSQL guards serialize the final authorization decision with writes."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.insights_results.routes import AUTHORITY_MODELS, release_authority
from tests.migrations.conftest_postgres import to_async_url


@pytest.fixture
async def authority_sessions(pg_url):
    engine = create_async_engine(to_async_url(pg_url))
    try:
        async with engine.begin() as connection:
            for model in AUTHORITY_MODELS:
                await connection.execute(text(f"CREATE TABLE {model.__table__.name} (id text PRIMARY KEY, state text)"))
        yield async_sessionmaker(engine)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_release_decision_guards_authority_updates_and_inserted_revocations(authority_sessions):
    tables = (
        "projects",
        "products",
        "product_project_bindings",
        "team_memberships",
        "tenant_memberships",
        "connector_cells",
        "connector_policy_realms",
        "connector_evidence_dependencies",
        "product_source_bindings",
        "user_credentials",
        "channel_tenant_map",
        "installation_revocations",
    )
    async with authority_sessions() as reader, authority_sessions() as writer, authority_sessions() as other_reader:
        async with writer.begin():
            await writer.execute(text("INSERT INTO projects VALUES ('example', 'active')"))
        async with release_authority(reader):
            async with release_authority(other_reader):
                assert await other_reader.scalar(text("SELECT state FROM projects WHERE id = 'example'")) == "active"
            for table in tables:
                with pytest.raises(DBAPIError) as blocked:
                    async with writer.begin():
                        await writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                        await writer.execute(text(f"INSERT INTO {table} VALUES ('revocation', 'revoked')"))
                assert blocked.value.orig.sqlstate == "55P03"
            with pytest.raises(DBAPIError) as blocked:
                async with writer.begin():
                    await writer.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    await writer.execute(text("UPDATE projects SET state = 'revoked' WHERE id = 'example'"))
            assert blocked.value.orig.sqlstate == "55P03"
            assert await reader.scalar(text("SELECT state FROM projects WHERE id = 'example'")) == "active"
        async with writer.begin():
            await writer.execute(text("UPDATE projects SET state = 'revoked' WHERE id = 'example'"))
        async with release_authority(reader):
            assert await reader.scalar(text("SELECT state FROM projects WHERE id = 'example'")) == "revoked"


@pytest.mark.asyncio
async def test_release_decision_fails_closed_on_an_active_authority_writer(authority_sessions):
    async with authority_sessions() as reader, authority_sessions() as writer:
        async with writer.begin():
            await writer.execute(text("INSERT INTO installation_revocations VALUES ('example', 'revoked')"))
            with pytest.raises(DBAPIError) as blocked:
                async with release_authority(reader):
                    pytest.fail("an unguarded release decision must not run")
            assert blocked.value.orig.sqlstate == "55P03"
        async with release_authority(reader):
            assert await reader.scalar(text("SELECT state FROM installation_revocations WHERE id = 'example'")) == "revoked"
