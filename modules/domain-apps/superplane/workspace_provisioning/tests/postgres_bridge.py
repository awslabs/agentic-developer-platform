"""A real PostgreSQL carrying the HARNESS schema, for the execution tests.

`workspace_bootstrap/tests/postgres_support.py` provides a disposable server carrying
the *domain* schema (the alembic chain), which is what the ownership-journal tests need.
The execution tests need a different schema on the same kind of server: `harness_jobs`'
own DDL, applied by `harness_jobs.apply`. Rather than widen that module — whose fixtures
are shaped around the migration chain and a synchronous `TransactionalStore` — this one
provides the harness half.

## Why the event loop is parked on a thread instead of using pytest-asyncio

The harness's surface is async (`open_operation`, `dispatch`, `acquire`). The domain
module declares **no `asyncio_mode`** in any pytest config, and its lane installs no
`pytest-asyncio` (see `superplane-domain-ci.yml`: it installs `modules/gateway[dev]`,
whose `pytest-asyncio` is present, but this module's own config never sets auto mode).
With no `asyncio_mode = "auto"` and no explicit marker, a bare `async def test_...`
is **collected, not run, and reported as passed** with only a warning — a whole suite of
execution guarantees reporting green while asserting nothing.

That is the manufactured-evidence failure this wave's contracts exist to prevent, and it
would be invisible in the pass count. So the tests here are synchronous functions that
submit a coroutine to a loop on a background thread and wait for it, exactly as
`workspace_bootstrap/tests/postgres_support.py:_Loop` already does in this module for the
same reason. A test that fails to run then fails, rather than passing quietly.

## Skip vs. fail

The fixtures skip lazily, inside the fixture, when no real server is obtainable — never
at module or conftest import, for the reason `postgres_support.py` documents at length (a
collection-time skip in a shared conftest takes the whole directory with it).

`WORKSPACE_PROVISIONING_REQUIRE_POSTGRES=1` converts that skip into a failure, matching
`HARNESS_JOBS_REQUIRE_POSTGRES` and `ACCOUNT_PROVISIONING_REQUIRE_POSTGRES`. A CI lane
sets it, because "N skipped" is green and a lane reporting green while asserting none of
these execution guarantees is worse than no lane at all.

## No credential, nothing outside the temporary directory

The server runs against a fresh temp data directory over a unix socket, created per test
module and destroyed in teardown; each test gets a freshly created database on it. No
DSN, host or password is read from the environment, so these tests cannot reach a real
database even by accident.
"""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager, contextmanager

import pytest

REQUIRE_VAR = "WORKSPACE_PROVISIONING_REQUIRE_POSTGRES"

_UNAVAILABLE = (
    "the retirement execution tests require a disposable PostgreSQL (the `pgserver` "
    "package) and the `asyncpg` driver. They assert constraint, transaction, advisory "
    "lock and fence-token behaviour that only a real database has, so there is nothing "
    "to fall back to."
)


def _unavailable():
    """Skip, or fail when the caller declared a database must be present."""
    if os.environ.get(REQUIRE_VAR):
        raise AssertionError(
            f"{REQUIRE_VAR} is set, so this suite must not skip. {_UNAVAILABLE}"
        )
    pytest.skip(_UNAVAILABLE, allow_module_level=False)


def _obtainable() -> bool:
    import importlib.util

    return all(
        importlib.util.find_spec(name) is not None
        for name in ("pgserver", "asyncpg", "harness_jobs")
    )


# `skipif(False)` when a database is required rather than dropping the mark, so the
# failure arrives from the fixture with the real diagnostic attached instead of from a
# collection-time condition. Same construction as `harness_jobs`' `requires_postgres`.
requires_harness_postgres = pytest.mark.skipif(
    not _obtainable() and not os.environ.get(REQUIRE_VAR),
    reason=_UNAVAILABLE,
)


class _Loop:
    """A background event loop, so synchronous tests can drive the async harness.

    Same construction and rationale as `workspace_bootstrap/tests/postgres_support.py`.
    The timeout is a deadlock detector rather than a performance budget: every operation
    is a millisecond-scale statement over a local unix socket, so reaching it means
    something is waiting on a lock that will not be released — which must fail the test
    rather than hang the lane.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coroutine, timeout: float = 60.0):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()


class Harness:
    """A real database with the harness schema applied, plus the seams it composes with.

    `connect` is the shape `OperationFacadeService` and `ExecutionRPCServer` are typed
    for: a callable returning an async context manager per call. A pool rather than one
    connection because `execute_provider` takes a dedicated connection for its advisory
    lock while short intent transactions commit independently on others — one shared
    connection would serialize those in the driver and the lock would stop meaning what
    it claims.
    """

    def __init__(self, loop: _Loop, pool) -> None:
        self._loop, self._pool = loop, pool

    def run(self, coroutine):
        """Drive one test's coroutine to completion on the background loop."""
        return self._loop.run(coroutine)

    def connect(self):
        @asynccontextmanager
        async def factory():
            async with self._pool.acquire() as held:
                yield held

        return factory()

    async def lease(self, operation_id, *, holder="worker", attempt="attempt-1"):
        """Acquire a real execution lease through the harness's own entry point."""
        from harness_jobs.leases import acquire

        async with self._pool.acquire() as connection:
            return await acquire(
                connection,
                operation_id=operation_id,
                holder=holder,
                attempt_id=attempt,
            )

    @classmethod
    @contextmanager
    def started(cls, tmp_path_factory, node_name: str):
        """A fresh database with the harness DDL applied, dropped afterwards."""
        if not _obtainable():
            _unavailable()
        import asyncpg
        import pgserver
        from harness_jobs import apply

        loop = _Loop()
        server = pgserver.get_server(
            tmp_path_factory.mktemp("workspace-provisioning-harness-pg")
        )
        base = server.get_uri()
        # The node name carries into the database name so a leaked database says which
        # test leaked it. Sanitized because parametrized node names contain brackets.
        suffix = "".join(c if c.isalnum() else "_" for c in node_name)[-40:]
        name = f"wp_{abs(hash(node_name)) % 10**10}_{suffix}".lower()[:60]
        quoted = '"' + name.replace('"', '""') + '"'

        admin = loop.run(asyncpg.connect(base))
        loop.run(admin.execute(f"DROP DATABASE IF EXISTS {quoted}"))
        loop.run(admin.execute(f"CREATE DATABASE {quoted}"))

        # Built INSIDE the loop's thread, not passed in as an awaitable: asyncpg's pool
        # binds the running event loop at construction, so constructing it out here
        # would attach it to whichever loop happens to be current on the main thread and
        # every later `acquire` would be on the wrong one.
        async def build():
            pool = await asyncpg.create_pool(
                base,
                database=name,
                min_size=1,
                max_size=10,
                # A deadlock fails in seconds with a clear error instead of hanging the
                # lane until the job timeout.
                server_settings={"statement_timeout": "20000"},
            )
            async with pool.acquire() as connection:
                await apply(connection)
            return pool

        pool = loop.run(build())
        try:
            yield cls(loop, pool)
        finally:
            loop.run(pool.close())
            loop.run(admin.execute(f"DROP DATABASE IF EXISTS {quoted}"))
            loop.run(admin.close())
            server.cleanup()
            loop.close()
