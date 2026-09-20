"""Real-database fixtures for the harness jobs store.

Issue #5525 (w6-02), EPIC #4910, Wave 6.

## Why these tests need a real database and not a fake

Every guarantee this package makes is a property of PostgreSQL's behaviour rather
than of Python:

* the duplicate refusal is a `UNIQUE` constraint firing under genuine concurrency;
* "admission and enqueue commit together" is transaction atomicity;
* the delivery claim is `FOR UPDATE SKIP LOCKED`, which has no meaning without a lock
  manager;
* "survives a restart" is a claim about what is on disk.

A fake, an in-memory dict, or SQLite would pass a suite that proves none of those.
SQLite in particular has no `SKIP LOCKED` and different constraint-timing semantics,
so a green run against it would be evidence about the fake. So: real PostgreSQL, or
skip and say so. Skipping is honest; substituting is not.

## Isolation

Each test gets its own randomly-named schema in the target database, created before
and dropped after. A schema rather than a database because creating a database
per test is slow and needs privileges a test user may not have, whereas
`search_path` isolation is enough -- the tables are unqualified in every statement,
so they resolve into the per-test schema.

`statement_timeout` is set so a test that deadlocks fails in seconds with a clear
error instead of hanging the lane.

## Where the database comes from

Two sources, in order:

1. `HARNESS_JOBS_TEST_POSTGRES_URL` -- an external disposable database. Same
   env-var-guarded convention as
   `src/superplane-api/tests/test_provider_handles_postgres.py`.
2. the `pgserver` package, which bundles a real PostgreSQL binary and runs it against
   a temporary data directory over a unix socket -- no Docker, no root, no listening
   port. This is how `modules/gateway/tests/migrations/conftest_postgres.py` gets a
   real server in CI, and the API calls here are the same three that module uses.

The second source is what lets a CI lane run this suite at all: ARC runners have no
Docker-in-Docker (`gateway-ci.yml:344`), so a service container is not available.

    HARNESS_JOBS_TEST_POSTGRES_URL=postgresql://user@host:5432/db \
        python3 -m pytest modules/harness/jobs/tests -v

## Why a skip has to be refusable

With neither source the suite skips, and a skip is the honest answer for a developer
without a database. It is the *wrong* answer for CI: "56 skipped" is green, and a lane
that reports green while asserting none of this package's actual guarantees is worse
than no lane -- it is the manufactured evidence this wave's contracts exist to
prevent. So `HARNESS_JOBS_REQUIRE_POSTGRES=1` converts the skip into a failure, and
the CI lane sets it. The default stays a skip because a developer is not CI.

No provider, cloud, AWS or B service is contacted by any test here.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from harness_jobs import apply

ENV_VAR = "HARNESS_JOBS_TEST_POSTGRES_URL"
REQUIRE_VAR = "HARNESS_JOBS_REQUIRE_POSTGRES"

_UNAVAILABLE = (
    f"requires a disposable PostgreSQL database: set {ENV_VAR}, or install the "
    "`pgserver` package (wheels exist for Python <= 3.12) to have one started for "
    "you. These tests assert constraint, transaction and lock behaviour that only a "
    "real database has, so there is nothing to fall back to."
)


def _database_is_obtainable() -> bool:
    """True when either source of a real server is present.

    Uses `find_spec` rather than importing: this runs at module import time in every
    test module, and importing `pgserver` unpacks its bundled binary.
    """
    if os.environ.get(ENV_VAR):
        return True
    return importlib.util.find_spec("pgserver") is not None


def _unavailable() -> None:
    """Skip, or fail if the caller declared that a database must be present."""
    if os.environ.get(REQUIRE_VAR):
        raise AssertionError(
            f"{REQUIRE_VAR} is set, so this suite must not skip. {_UNAVAILABLE}"
        )
    pytest.skip(_UNAVAILABLE)


# Applied at module scope in each test module that needs a database. Kept here so the
# reason is written once.
#
# `skipif(False)` when a database is required, rather than dropping the mark: the mark
# is what the test modules reference, and the failure then comes from the fixture with
# the real diagnostic attached instead of from a collection-time condition.
requires_postgres = pytest.mark.skipif(
    not _database_is_obtainable() and not os.environ.get(REQUIRE_VAR),
    reason=_UNAVAILABLE,
)


@pytest.fixture(scope="session")
def postgres_server(tmp_path_factory) -> AsyncIterator[str]:
    """A base DSN for a real server, from the environment or from `pgserver`.

    Session-scoped because starting a server costs seconds; per-test isolation is a
    fresh schema on it (see `pool`), not a fresh server.

    Not a fixture the tests take directly -- `postgres_url()` reads the resolved value
    -- because the helper is also called from inside a handful of tests that open their
    own second connection.
    """
    global _RESOLVED_URL
    external = os.environ.get(ENV_VAR)
    if external:
        _RESOLVED_URL = external.replace("postgresql+asyncpg://", "postgresql://")
        yield _RESOLVED_URL
        return

    if importlib.util.find_spec("pgserver") is None:
        _unavailable()

    import pgserver

    data_dir = tmp_path_factory.mktemp("harness-jobs-pgdata")
    server = pgserver.get_server(str(data_dir))
    try:
        _RESOLVED_URL = server.get_uri()
        yield _RESOLVED_URL
    finally:
        _RESOLVED_URL = None
        server.cleanup()


# Set by `postgres_server`. A module-level value rather than a fixture return because
# `postgres_url()` is called from inside test bodies that already hold a pool and need
# the DSN again to open an independent connection (the restart tests), where taking
# another fixture would mean threading it through every one of them.
_RESOLVED_URL: str | None = None


def postgres_url() -> str:
    """The resolved database URL, or skip/fail per `HARNESS_JOBS_REQUIRE_POSTGRES`.

    asyncpg takes a plain `postgresql://` DSN; a `postgresql+asyncpg://` SQLAlchemy
    URL is normalized so the same value works for both this suite and the domain
    app's SQLAlchemy suites.
    """
    if _RESOLVED_URL:
        return _RESOLVED_URL
    url = os.environ.get(ENV_VAR)
    if not url:
        _unavailable()
    return url.replace("postgresql+asyncpg://", "postgresql://")  # type: ignore[union-attr]


@pytest.fixture
async def schema_name() -> str:
    """A unique schema name for one test."""
    return "harness_jobs_test_" + uuid.uuid4().hex


@pytest.fixture
async def pool(postgres_server: str, schema_name: str) -> AsyncIterator[object]:
    """An asyncpg pool bound to a fresh schema, with the store's DDL applied.

    A pool rather than a single connection because the concurrency tests need
    genuinely separate connections -- two coroutines sharing one connection are
    serialized by the driver, so a "concurrent duplicate create" test on a single
    connection would prove nothing about the constraint.

    Takes `postgres_server` so the session server is started before the first pool is
    built; the DSN itself comes from `postgres_url()`, which several test bodies also
    call directly.
    """
    asyncpg = pytest.importorskip(
        "asyncpg", reason="asyncpg is required for the real-database suite"
    )
    url = postgres_url()

    admin = await asyncpg.connect(url)
    try:
        await admin.execute(f'CREATE SCHEMA "{schema_name}"')
    finally:
        await admin.close()

    created = await asyncpg.create_pool(
        url,
        min_size=1,
        max_size=10,
        server_settings={
            "search_path": schema_name,
            # A deadlock or a lock wait fails fast and visibly rather than hanging
            # the lane until the job timeout.
            "statement_timeout": "15000",
        },
    )
    assert created is not None
    async with created.acquire() as connection:
        await apply(connection)
    try:
        yield created
    finally:
        await created.close()
        cleanup = await asyncpg.connect(url)
        try:
            await cleanup.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        finally:
            await cleanup.close()


@pytest.fixture
async def connection(pool: object) -> AsyncIterator[object]:
    """One connection from the pool, for tests that need only one."""
    async with pool.acquire() as held:  # type: ignore[attr-defined]
        yield held


@pytest.fixture
def connect(pool: object):
    """A `connect` callable in the shape `OperationFacadeService` expects.

    Returns a factory producing an async context manager per call, which is what the
    facade's composition seam is typed as.
    """

    @asynccontextmanager
    async def factory():
        async with pool.acquire() as held:  # type: ignore[attr-defined]
            yield held

    return factory
