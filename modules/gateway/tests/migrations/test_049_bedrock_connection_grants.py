"""Exercise explicit link constraints and safe downgrade on SQLite and PostgreSQL."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.shared.models.bedrock_routing import BedrockConnectionGrant

from .conftest_postgres import run_alembic, upgrade

path = Path(__file__).resolve().parents[2] / "alembic/versions/049_bedrock_connection_grants.py"
spec = importlib.util.spec_from_file_location("migration_049", path)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@pytest.fixture(params=["sqlite", "postgresql"])
def connection(request, monkeypatch):
    engine = sa.create_engine(request.getfixturevalue("pg_url") if request.param == "postgresql" else "sqlite://")
    with engine.begin() as conn:
        if request.param == "sqlite":
            conn.execute(sa.text("PRAGMA foreign_keys=ON"))
        conn.execute(sa.text("CREATE TABLE bedrock_destination_registry (id VARCHAR(255) PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO bedrock_destination_registry VALUES ('d1'), ('d2'), ('d3')"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(conn)))
        migration.upgrade()
        yield conn
    engine.dispose()


def insert(connection, destination="d1", credential="c1", org="org1"):
    connection.execute(
        sa.text(
            "INSERT INTO bedrock_connection_grants (destination_id, credential_id, org_id, created_by_user_id, created_at) "
            "VALUES (:destination, :credential, :org, 'admin', CURRENT_TIMESTAMP)"
        ),
        {"destination": destination, "credential": credential, "org": org},
    )


def test_pair_uniqueness_and_destination_fk(connection):
    insert(connection)
    with pytest.raises(IntegrityError), connection.begin_nested():
        insert(connection, destination="d2")
    insert(connection, destination="d2", org="org2")
    with pytest.raises(IntegrityError), connection.begin_nested():
        insert(connection, destination="missing", credential="c2")
    connection.execute(sa.text("DELETE FROM bedrock_destination_registry WHERE id='d1'"))
    assert connection.scalar(sa.text("SELECT count(*) FROM bedrock_connection_grants")) == 1
    assert connection.scalar(sa.text("SELECT org_id FROM bedrock_connection_grants")) == "org2"


def test_model_migration_parity_and_guarded_downgrade(connection):
    inspector = sa.inspect(connection)
    columns = inspector.get_columns("bedrock_connection_grants")
    model = BedrockConnectionGrant.__table__
    assert {c["name"]: c["nullable"] for c in columns} == {c.name: c.nullable for c in model.columns}
    assert {c["name"]: str(c["type"].compile(dialect=connection.dialect)) for c in columns} == {
        c.name: str(c.type.compile(dialect=connection.dialect)) for c in model.columns
    }
    assert inspector.get_unique_constraints(model.name)[0]["column_names"] == ["credential_id", "org_id"]
    fk = inspector.get_foreign_keys(model.name)[0]
    assert fk["referred_table"] == "bedrock_destination_registry"
    assert fk["options"]["ondelete"] == "CASCADE"
    insert(connection)
    with pytest.raises(RuntimeError, match="Remove existing-connection"):
        migration.downgrade()
    assert connection.scalar(sa.text("SELECT count(*) FROM bedrock_connection_grants")) == 1
    connection.execute(sa.text("DELETE FROM bedrock_connection_grants"))
    migration.downgrade()
    assert model.name not in sa.inspect(connection).get_table_names()
    migration.upgrade()
    assert connection.scalar(sa.text("SELECT count(*) FROM bedrock_connection_grants")) == 0


def test_real_postgres_migration_chain(pg_url):
    upgrade(pg_url, migration.revision)
    assert len(migration.revision) <= 32
    result = run_alembic(pg_url, "downgrade", migration.down_revision)
    assert result.returncode == 0, result.stdout + result.stderr
    upgrade(pg_url, migration.revision)


async def test_concurrent_link_retries_and_mapping_unlink_serialize(pg_url, monkeypatch):
    """Real PostgreSQL locks: retries share one grant; unlink waits for map saves."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.shared.models.organization import Department, Organization, Team, User
    from src.shared.models.vault import UserCredential
    from tests.admin.bedrock_routing.conftest import PLATFORM_ADMIN_ID, PLATFORM_ADMIN_SUB, client_for, platform_admin_context

    from .conftest_postgres import to_async_url

    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add_all([Organization(id="source", name="Source"), Organization(id="target", name="Target")])
        await db.flush()
        db.add(Department(id="dept", org_id="source", name="Department"))
        await db.flush()
        db.add(Team(id="team", org_id="source", department_id="dept", name="Team"))
        await db.flush()
        db.add(User(id=PLATFORM_ADMIN_ID, cognito_sub=PLATFORM_ADMIN_SUB, org_id="source", team_id="team", email="admin@example.com"))
        await db.flush()
        db.add(
            UserCredential(
                id="credential",
                org_id="source",
                user_id=PLATFORM_ADMIN_ID,
                service="aws",
                credential_type="aws_role",
                label="AWS",
                secret_arn="test-secret",
                scopes={"status": "verified", "account_id": "123456789012", "role_arn": "arn:aws:iam::123456789012:role/test"},
            )
        )
        await db.commit()

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def probe(**kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return True, None

    monkeypatch.setattr("src.admin.bedrock_routing.service.probe_routing_destination", probe)
    prefix = "/admin/bedrock-routing"

    async def request(method, path, body=None):
        async with sessions() as db, client_for(db, platform_admin_context()) as client:
            return await client.request(method, prefix + path, json=body)

    body = {"source": "shared_connection", "credential_id": "credential", "link_to_org_id": "target"}
    tasks = []
    try:
        first = asyncio.create_task(request("POST", "/connection-links", body))
        tasks.append(first)
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(request("POST", "/connection-links", body))
        tasks.append(second)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.1)
        assert calls == 1
        release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert [r.status_code for r in responses] == [201, 201], [r.text for r in responses]
        dest_id = responses[0].json()["destination"]["id"]
        assert responses[1].json()["destination"]["id"] == dest_id
        async with sessions() as db:
            assert await db.scalar(sa.select(sa.func.count()).select_from(BedrockConnectionGrant)) == 1

        entered.clear()
        release.clear()
        mapping = asyncio.create_task(request("PUT", "/mappings/org:target", {"destination_id": dest_id}))
        tasks.append(mapping)
        await asyncio.wait_for(entered.wait(), 5)
        unlink = asyncio.create_task(request("DELETE", f"/connection-links/{dest_id}"))
        tasks.append(unlink)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(unlink), 0.1)
        release.set()
        mapped, removed = await asyncio.wait_for(asyncio.gather(mapping, unlink), 5)
        assert mapped.status_code == 200, mapped.text
        assert removed.status_code == 409, removed.text
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
