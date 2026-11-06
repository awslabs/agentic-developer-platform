"""Real PostgreSQL 16 fixtures for migration tests (issue #4969, design §6).

Why this exists instead of the SQLite fixtures the older migration tests use:
the V2 pricing schema is built almost entirely out of things SQLite does not
have. `NUMERIC(14,10)` precision, `JSONB`, `GENERATED ALWAYS AS IDENTITY`,
plpgsql triggers, `SELECT ... FOR UPDATE` row locking, and the `42P01`/`42703`
error codes the feature-detecting readers depend on are all PostgreSQL-specific.
A SQLite run would report green while testing none of the behavior that matters,
which is precisely why the design note requires "isolated PostgreSQL 16 with
actual migrations, not SQLite".

The server is provided by the `pgserver` package: a real PostgreSQL 16 binary run
against a temporary data directory, requiring no root, no Docker and no listening
port on the host. It is a test-only dependency.

Tests that need a database take the `pg_url` fixture (a fresh, empty database per
test) or `migrated_url` (upgraded to a given revision).
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

GATEWAY_ROOT = Path(__file__).resolve().parents[2]

# Imported lazily inside the fixtures, NOT with a module-level
# `pytest.importorskip`. This module is re-exported by conftest.py so the fixtures
# are auto-discovered, and a skip raised at conftest import time aborts collection
# for the WHOLE directory — which silently took the 413 pre-existing (SQLite-based)
# migration tests with it on any interpreter without pgserver. A skip must apply to
# the tests that actually need a PostgreSQL server, and nothing else.
_UNAVAILABLE = "real-PostgreSQL migration tests require the pgserver package (Python <= 3.12); see tests/migrations/README-postgres.md"
_SESSION_SERVERS = pytest.StashKey[dict]()
_SESSION_SCHEMAS = pytest.StashKey[dict]()


def _require_pgserver():
    try:
        import pgserver
    except ImportError:
        pytest.skip(_UNAVAILABLE, allow_module_level=False)
    return pgserver


def _require_psycopg2():
    try:
        import psycopg2
    except ImportError:
        pytest.skip("real-PostgreSQL migration tests require psycopg2")
    return psycopg2


# Migration 016 runs `CREATE EXTENSION IF NOT EXISTS pgcrypto`, which RDS ships
# but the pgserver build does not (its extension directory contains only plpgsql
# and vector). Without this the whole chain aborts at 016 and nothing downstream —
# including the V2 pricing schema — can be tested against a real server at all.
#
# The shim is sound rather than merely expedient: 016's own comment says pgcrypto
# is there to provide `gen_random_uuid()` "as a belt-and-suspenders default", and
# that function has been in PostgreSQL core since version 13, so on a 16 server it
# resolves identically with or without the extension. Nothing in alembic/ or src/
# calls any other pgcrypto function (no digest/crypt/gen_salt/hmac/pgp_*), so the
# shim's empty body cannot mask a missing symbol — and the DO block below fails
# loudly at CREATE EXTENSION time if a future PostgreSQL ever drops the core
# function, instead of letting a migration silently produce NULL ids.
#
# This is a test-harness accommodation only. It writes into the installed pgserver
# package, never into the repo, and changes nothing about production, where the
# real extension is present.
_PGCRYPTO_CONTROL = "\n".join(
    [
        "comment = 'pgcrypto shim (test harness): gen_random_uuid() from PostgreSQL core'",
        "default_version = '1.3'",
        "relocatable = true",
        "",
    ]
)
_PGCRYPTO_SQL = "\n".join(
    [
        "-- Test-harness shim; see tests/migrations/conftest_postgres.py.",
        "-- Asserts the core function 016 actually depends on exists.",
        "DO $$ BEGIN PERFORM gen_random_uuid(); END $$;",
        "",
    ]
)


def _ensure_pgcrypto_shim(pgserver) -> None:
    """Make `CREATE EXTENSION pgcrypto` resolvable on the embedded server."""
    extension_dir = Path(inspect.getfile(pgserver)).parent / "pginstall/share/postgresql/extension"
    if not extension_dir.is_dir():
        pytest.skip(f"pgserver extension directory not found at {extension_dir}")
    control = extension_dir / "pgcrypto.control"
    if control.exists():
        # A real pgcrypto (or a shim from an earlier run) is already installed.
        return
    try:
        control.write_text(_PGCRYPTO_CONTROL)
        (extension_dir / "pgcrypto--1.3.sql").write_text(_PGCRYPTO_SQL)
    except OSError as exc:
        pytest.skip(f"cannot install the pgcrypto test shim into {extension_dir}: {exc}")


class _ExternalServer:
    """A server this process did not start, addressed by URI.

    Exists so these tests can run on an interpreter where ``pgserver`` cannot be
    imported. ``pgserver`` ships a PostgreSQL binary only for Python <= 3.12, so on
    3.13 every real-PostgreSQL test *silently skips* — and a skipped
    transaction-isolation test looks identical to a passing one in a CI summary.
    Pointing ``BG_TEST_POSTGRES_URI`` at an already-running server (started from any
    interpreter, or a service container) keeps the evidence real.
    """

    def __init__(self, uri: str) -> None:
        self._uri = uri

    def get_uri(self, database: str | None = None) -> str:
        # Mirrors pgserver's signature: per-test databases are created on the
        # server and addressed by swapping the database in the URI.
        if not database:
            return self._uri
        base, _, query = self._uri.partition("?")
        base = base.rsplit("/", 1)[0] + "/" + database
        return base + ("?" + query if query else "")

    def cleanup(self) -> None:  # the owner of an external server stops it
        return None


@pytest.fixture(scope="session")
def pg_server(request, tmp_path_factory):
    """One PostgreSQL 16 instance for the whole session.

    Session-scoped because initdb costs a few seconds; per-test isolation comes
    from creating a separate database on it rather than a separate server.
    """
    # Re-exporting a fixture from a test module gives it another FixtureDef and
    # therefore another session cache. Keep the server on the actual Session so
    # all aliases share it, including under xdist (one Session per worker).
    servers = request.session.stash.setdefault(_SESSION_SERVERS, {})
    external = os.environ.get("BG_TEST_POSTGRES_URI")
    if external not in servers:
        _require_psycopg2()
        if external:
            server = _ExternalServer(external)
        else:
            pgserver = _require_pgserver()
            _ensure_pgcrypto_shim(pgserver)
            data_dir = tmp_path_factory.mktemp("pgdata")
            server = pgserver.get_server(str(data_dir))
        servers[external] = server
        request.session.addfinalizer(server.cleanup)
    return servers[external]


@contextmanager
def postgres_database(pg_server, *, template=None):
    """An owned database; never drop or terminate connections to its template."""
    psycopg2 = _require_psycopg2()
    name = f"t{uuid.uuid4().hex[:16]}"
    admin = pg_server.get_uri()

    connection = psycopg2.connect(admin)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            from psycopg2 import sql

            statement = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
            if template:
                statement += sql.SQL(" TEMPLATE {}").format(sql.Identifier(template))
            cursor.execute(statement)
    finally:
        connection.close()

    try:
        yield pg_server.get_uri(database=name)
    finally:
        connection = psycopg2.connect(admin)
        connection.autocommit = True
        try:
            with connection.cursor() as cursor:
                # Terminate stragglers before dropping only this owned database.
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            connection.close()


@pytest.fixture
def pg_url(pg_server):
    """Fresh empty database for tests which exercise migrations or custom DDL."""
    with postgres_database(pg_server) as url:
        yield url


@pytest.fixture
def pg_head_url(request, pg_server):
    """Fresh clone of a migrated schema for business tests, with no shared data.

    Migration tests keep using pg_url and the real upgrade/downgrade helpers.
    Template ownership follows the actual pytest Session, including fixture aliases.
    """
    from tests.postgres_schema import PostgresSchemas

    schemas = request.session.stash.setdefault(_SESSION_SCHEMAS, {})
    key = pg_server.get_uri()
    if key not in schemas:
        schemas[key] = PostgresSchemas(pg_server, GATEWAY_ROOT, postgres_database, upgrade)
        request.session.addfinalizer(schemas[key].close)
    with schemas[key].database() as url:
        yield url


def to_async_url(url: str) -> str:
    """Rewrite a psycopg2 URL to the asyncpg driver alembic/env.py expects."""
    for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix) :]
    return url


def run_alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    """Run the real Alembic CLI against `url`, in a subprocess.

    A subprocess, not `alembic.command`, for two reasons: the app's env.py reads
    configuration from the process environment, and an in-process run would leave
    imported migration modules and engine state behind to affect later tests.

    ``BG_DATABASE_URL`` is the variable env.py actually consults (see
    ``get_database_url_for_alembic``); ``DATABASE_URL`` is ignored there, so
    setting only that one would silently run against the alembic.ini default.
    """
    # setup-python's relocated interpreter needs its library search path. Without
    # it a runner can load a different libpython and lose the installed packages.
    # Preserve interpreter configuration without inheriting production DB/AWS env.
    runtime_env = {name: os.environ[name] for name in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "PYTHONHOME", "PYTHONUSERBASE") if name in os.environ}
    python_paths = [GATEWAY_ROOT]
    # Packaged qualification mounts tests at /app/tests and uses installed
    # dependencies. Add sibling sources only for the repository checkout layout.
    if GATEWAY_ROOT.name == "gateway" and GATEWAY_ROOT.parent.name == "modules":
        python_paths.extend(
            path
            for path in (
                GATEWAY_ROOT.parent / "L5-services/src",
                GATEWAY_ROOT.parent.parent / "libs/python/adp-common/src",
            )
            if path.is_dir()
        )
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=GATEWAY_ROOT,
        capture_output=True,
        text=True,
        env={
            **runtime_env,
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "BG_DATABASE_URL": to_async_url(url),
            "BG_RDS_IAM_AUTH": "false",
            "PYTHONPATH": os.pathsep.join(map(str, python_paths)),
        },
        check=False,
    )


def upgrade(url: str, revision: str = "head") -> None:
    """Upgrade to `revision`, failing the test loudly with Alembic's own output."""
    result = run_alembic(url, "upgrade", revision)
    if result.returncode != 0:
        raise AssertionError(f"alembic upgrade {revision} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")


def downgrade(url: str, revision: str) -> None:
    result = run_alembic(url, "downgrade", revision)
    if result.returncode != 0:
        raise AssertionError(f"alembic downgrade {revision} failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}")


@pytest.fixture
def connect(pg_url):
    """Factory for psycopg2 connections to the test database."""
    psycopg2 = _require_psycopg2()
    opened = []

    def _connect(autocommit: bool = True):
        connection = psycopg2.connect(pg_url)
        connection.autocommit = autocommit
        opened.append(connection)
        return connection

    yield _connect

    for connection in opened:
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - cleanup must not mask a test failure
            pass
