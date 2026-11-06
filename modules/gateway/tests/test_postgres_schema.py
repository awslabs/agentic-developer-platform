"""Real PostgreSQL schema cloning preserves test isolation and ownership."""

import psycopg2
import pytest

from tests.migrations.conftest_postgres import postgres_database
from tests.postgres_schema import PostgresSchemas, source_signature

pytest_plugins = ("tests.migrations.conftest_postgres",)


@pytest.fixture
def source(tmp_path):
    gateway = tmp_path / "modules/gateway"
    (gateway / "alembic/versions").mkdir(parents=True)
    (gateway / "alembic.ini").write_text("[alembic]\n")
    (gateway / "pyproject.toml").write_text("")
    (gateway / "alembic/versions/one.py").write_text("revision = 'one'\n")
    return gateway


def databases(server):
    with psycopg2.connect(server.get_uri()) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT datname FROM pg_database")
        return {row[0] for row in cursor}


def migrate(url):
    with psycopg2.connect(url) as connection, connection.cursor() as cursor:
        cursor.execute("CREATE TABLE items (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, value text UNIQUE NOT NULL)")
        cursor.execute("INSERT INTO items(value) VALUES ('seed')")


def test_clones_isolate_commits_ddl_sequences_and_leaked_connections(pg_server, source):
    before = databases(pg_server)
    calls = []

    def upgrade(url):
        calls.append(url)
        migrate(url)

    schemas = PostgresSchemas(pg_server, source, postgres_database, upgrade)
    try:
        with schemas.database() as first:
            with psycopg2.connect(first) as connection, connection.cursor() as cursor:
                cursor.execute("INSERT INTO items(value) VALUES ('first') RETURNING id")
                assert cursor.fetchone() == (2,)
                cursor.execute("CREATE TABLE private_data (value text)")
            leaked = psycopg2.connect(first)
        with pytest.raises(psycopg2.OperationalError), leaked.cursor() as cursor:
            cursor.execute("SELECT 1")
        leaked.close()
        with schemas.database() as second:
            assert second != first
            with psycopg2.connect(second) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT * FROM items")
                assert cursor.fetchall() == [(1, "seed")]
                cursor.execute("SELECT to_regclass('private_data')")
                assert cursor.fetchone() == (None,)
                cursor.execute("INSERT INTO items(value) VALUES ('second') RETURNING id")
                assert cursor.fetchone() == (2,)
            with pytest.raises(psycopg2.IntegrityError), psycopg2.connect(second) as connection, connection.cursor() as cursor:
                cursor.execute("INSERT INTO items(value) VALUES ('seed')")
        assert len(calls) == 1
        with pytest.raises(psycopg2.OperationalError):
            psycopg2.connect(calls[0], connect_timeout=1)
    finally:
        schemas.close()
    assert databases(pg_server) == before


def test_changed_migration_sources_build_a_new_template(pg_server, source):
    before = databases(pg_server)
    calls = []

    def upgrade(url):
        calls.append(url)
        migrate(url)

    schemas = PostgresSchemas(pg_server, source, postgres_database, upgrade)
    try:
        with schemas.database():
            pass
        original = source_signature(source)
        (source / "alembic/versions/one.py").write_text("revision = 'two'\n")
        assert source_signature(source) != original
        with schemas.database():
            pass
        assert len(calls) == 2
    finally:
        schemas.close()
    assert databases(pg_server) == before


def test_failed_template_is_removed_and_can_be_retried(pg_server, source):
    before = databases(pg_server)

    def broken(url):
        migrate(url)
        raise RuntimeError("migration failed")

    schemas = PostgresSchemas(pg_server, source, postgres_database, broken)
    with pytest.raises(RuntimeError, match="migration failed"), schemas.database():
        pytest.fail("Failed migration must not create a test clone")
    assert databases(pg_server) == before

    schemas.upgrade = migrate
    try:
        with schemas.database():
            pass
    finally:
        schemas.close()
    assert databases(pg_server) == before


def test_external_server_and_unrelated_database_survive_template_cleanup(pg_server, source):
    from tests.migrations.conftest_postgres import _ExternalServer

    external = _ExternalServer(pg_server.get_uri())
    with postgres_database(pg_server) as unrelated:
        migrate(unrelated)
        before = databases(pg_server)
        schemas = PostgresSchemas(external, source, postgres_database, migrate)
        try:
            with schemas.database():
                pass
        finally:
            schemas.close()
            external.cleanup()
        assert databases(pg_server) == before
        with psycopg2.connect(unrelated) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT value FROM items")
            assert cursor.fetchall() == [("seed",)]
