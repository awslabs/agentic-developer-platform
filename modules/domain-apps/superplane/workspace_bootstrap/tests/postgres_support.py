"""A disposable PostgreSQL and a real `TransactionalStore` over it — F10/F11 support.

Issue #5533 (w6-10), EPIC #4910. Added by the F10/F11 repair.

## Why the offline suite is not enough, stated precisely

Every other test in this directory drives `SqlRegistrationStore` through `_Recorder`, a
scripted double. That double is deliberately literal — it records whether a statement ran
inside a transaction, and it replies per statement — and it is what makes the reserve /
finalize / release contract checkable with no database. But there are exactly three
claims it CANNOT check, and all three are load-bearing:

1. **The statements are valid SQL against a schema that exists.** Review finding F11: the
   store's SQL had never met a real table. A scripted double replies to
   `SELECT ... FOR UPDATE` without parsing it, so a misspelled column or a table no
   migration creates passes every offline test.
2. **The advisory lock actually excludes.** `_Recorder` runs statements in one thread and
   returns immediately; there is no second connection for it to block. The F10 finding is
   precisely about what two concurrent processes see, and a single-threaded double cannot
   produce a second process.
3. **The reservation row is durable across connections.** The F5 interruption case is a
   process that dies. `_Recorder` holds its rows in a Python dict, which is gone with the
   process — the exact property the row exists to not have.

So this module provides a real PostgreSQL server and a real driver-backed store, and the
tests that use it assert only things the double cannot reach.

## Why the fixtures skip lazily rather than at import

`pgserver` ships a PostgreSQL binary and requires no root, no Docker and no listening
port, but it is not installable on every interpreter (it has no Python 3.13 wheel at time
of writing, and this package's own CI lane installs no database driver). The skip
therefore happens INSIDE the fixtures, never at module level and never in `conftest.py`.

That is not a style preference. A `pytest.importorskip` at conftest import time raises
during collection, and a collection-time skip in a shared conftest aborts collection for
the WHOLE directory — it would take all 495 offline tests with it on any interpreter
without `pgserver`, and the lane would report a skip where it used to report a suite.
`modules/gateway/tests/migrations/conftest_postgres.py` documents having hit exactly that.
This file is named `postgres_support.py` rather than `conftest_postgres.py` for the same
reason: nothing auto-imports it, so only the tests that ask for a database can be
affected by whether one is available.

## No credential, and nothing outside the temporary directory

The server runs against a fresh temporary data directory over a unix socket, is created
per module and destroyed in the fixture's teardown. Every connection is to that server.
There is no DSN, password or host read from the environment anywhere in this file, so
these tests cannot reach a real database even by accident — which is what makes them
consistent with this story's "no live migration, no live system" constraint: the schema is
applied to a database that did not exist when the test started and does not exist after.
"""

from __future__ import annotations

import asyncio
import io
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

MODULE_ROOT = Path(__file__).resolve().parents[2]
API_ROOT = MODULE_ROOT / "src" / "superplane-api"

_NO_PGSERVER = (
    "real-PostgreSQL tests require the `pgserver` package (no wheel for this "
    "interpreter); the offline suite covers the same contract with a scripted double"
)
_NO_ASYNCPG = "real-PostgreSQL tests require the `asyncpg` driver"
_NO_SQLALCHEMY = (
    "real-PostgreSQL tests require SQLAlchemy to compile named parameters for asyncpg"
)
_NO_ALEMBIC = "the migration schema test requires Alembic"


def require_pgserver():
    try:
        import pgserver
    except ImportError:
        pytest.skip(_NO_PGSERVER, allow_module_level=False)
    return pgserver


def require_asyncpg():
    try:
        import asyncpg
    except ImportError:
        pytest.skip(_NO_ASYNCPG, allow_module_level=False)
    return asyncpg


def require_sqlalchemy():
    try:
        import sqlalchemy
    except ImportError:
        pytest.skip(_NO_SQLALCHEMY, allow_module_level=False)
    return sqlalchemy


def require_alembic():
    try:
        import alembic.config
        import alembic.migration
        import alembic.operations
        import alembic.script
    except ImportError:
        pytest.skip(_NO_ALEMBIC, allow_module_level=False)
    return alembic


# --- the migration chain, rendered offline -------------------------------------


def render_migration_ddl(
    *, upgrade_only: bool = True, downgrade_revision: str | None = None
) -> tuple[str, str]:
    """Render the alembic chain to PostgreSQL DDL without connecting to anything.

    Returns `(upgrade_ddl_for_the_whole_chain, downgrade_ddl_for_the_named_revision)`.

    `downgrade_revision` must be given whenever a downgrade is requested, and naming it is
    required rather than convenient. This function used to render `revisions[-1].downgrade`
    -- the tail of the chain -- which silently meant "this story's revision" only for as
    long as this story's revision happened to be the last one. When #5673 added `018` the
    caller went on asking for a downgrade and received the DDL for a DIFFERENT revision:
    the table under test was never dropped, so the assertion failed with "the table still
    exists" and pointed at the schema rather than at this helper. A revision id cannot
    drift that way.

    Alembic's offline (`--sql`) mode is used rather than `alembic upgrade head` against a
    live URL, because the rendering must not require a connection: the same function is
    what lets the schema be inspected on an interpreter with no driver at all. The DDL is
    then applied by the caller to a disposable server, which is where it stops being a
    compile check and becomes an apply check.

    Deliberately NOT `command.upgrade(..., sql=True)`: that writes to stdout and emits
    `BEGIN`/`COMMIT` framing and `alembic_version` bookkeeping around each step, which the
    caller would have to strip. Driving `MigrationContext` directly renders exactly the
    operations each revision's `upgrade()` performs, which is the thing under test —
    `src/superplane-api/tests/test_migrations.py` establishes this idiom.
    """
    alembic = require_alembic()
    sa = require_sqlalchemy()

    config = alembic.config.Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    directory = alembic.script.ScriptDirectory.from_config(config)
    # `walk_revisions` yields newest first; migrations apply oldest first.
    revisions = list(reversed(list(directory.walk_revisions())))

    def render(operations) -> str:
        buffer = io.StringIO()
        engine = sa.create_mock_engine("postgresql://", lambda sql, *a, **k: None)
        context = alembic.migration.MigrationContext.configure(
            dialect=engine.dialect,
            opts={
                "as_sql": True,
                "output_buffer": buffer,
                # Off because the caller runs the whole chain as one statement batch on a
                # connection it controls. Leaving it on interleaves BEGIN/COMMIT into the
                # rendered text, which asyncpg would then reject inside its own
                # transaction.
                "transactional_ddl": False,
            },
        )
        with alembic.operations.Operations.context(context):
            for operation in operations:
                operation()
        return buffer.getvalue()

    upgrade = render([revision.module.upgrade for revision in revisions])
    if upgrade_only:
        return upgrade, ""
    if downgrade_revision is None:
        raise AssertionError(
            "render_migration_ddl(upgrade_only=False) requires downgrade_revision: "
            "the revision to reverse must be named, not inferred from chain position"
        )
    named = [
        revision for revision in revisions if revision.revision == downgrade_revision
    ]
    if not named:
        raise AssertionError(
            f"revision {downgrade_revision!r} is not in the chain Alembic walks; "
            f"the chain ends at {revisions[-1].revision!r}"
        )
    return upgrade, render([named[0].module.downgrade])


def walked_revision(revision_id: str):
    """The named revision, proven to be one Alembic actually walks to reach the head.

    What the schema test needs to rule out is an ORPHAN: a revision file that exists but
    that no `upgrade` walks, which therefore creates no table while every offline assertion
    about it still passes. Being the head is one way to be reachable, and that is how this
    was originally written — but it is not the property, and conflating the two meant every
    later migration anywhere in the chain broke a test about this story's table. `018`
    (#5673) is what surfaced it.

    Reachability is asserted directly instead: the chain has a single head, and the named
    revision appears in the walk from base to that head.
    """
    alembic = require_alembic()
    config = alembic.config.Config(str(API_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(API_ROOT / "alembic"))
    directory = alembic.script.ScriptDirectory.from_config(config)
    heads = directory.get_heads()
    assert len(heads) == 1, f"the migration chain must have one head; found {heads}"
    walked = {revision.revision for revision in directory.walk_revisions()}
    assert revision_id in walked, (
        f"revision {revision_id!r} is not reachable from head {heads[0]!r}: "
        "an unwalked revision creates nothing, and every offline assertion still passes"
    )
    return directory.get_revision(revision_id)


# --- the event loop, parked on its own thread ----------------------------------


class _Loop:
    """A background event loop, so a SYNCHRONOUS store can drive an async driver.

    `TransactionalStore.execute` is synchronous by contract, because the package it serves
    is standard-library-only and has no async surface. The only driver available here is
    `asyncpg`. Rather than change the production Protocol to suit a test dependency — which
    would be the test driving the design — the loop runs on its own thread and each
    statement is submitted to it and waited for.

    This matters for the concurrency tests specifically: two stores get two connections and
    both submit to the same loop, so when store A holds the advisory lock, store B's
    submission is genuinely pending on the database rather than queued behind A in Python.
    A single-threaded `asyncio.run` per statement could not express that at all.
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

    def run(self, coroutine, timeout: float = 30.0):
        """Submit a coroutine and wait for it.

        The timeout is a deadlock detector, not a performance budget: every operation here
        is a millisecond-scale statement against a local unix socket, so 30s can only be
        reached by something waiting on a lock that will never be released. Without it a
        lock-ordering mistake in a test would hang the whole lane instead of failing it.
        """
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout)

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()


# --- the real TransactionalStore ------------------------------------------------


class AsyncpgStore:
    """A real `TransactionalStore`: one asyncpg connection, explicit transactions.

    This is the shape the production adapter takes — `registry.py` documents that the
    connection arrives already authenticated, as `installation_bootstrap.py` receives its
    session — reduced to the two methods the Protocol requires.

    `named_to_positional` is the only translation. `registry.py`'s statements use named
    parameters (`:workspace_id`) because that is what SQLAlchemy's `text()` and the domain
    API's session take; asyncpg speaks `$1`. The conversion goes through SQLAlchemy's own
    compiler rather than a regular expression, so a statement is translated by the same
    code that would translate it in production and a test cannot pass because the test's
    parameter substitution was more forgiving than the real one.
    """

    def __init__(self, loop: _Loop, connection) -> None:
        self._loop = loop
        self._connection = connection
        self._transaction = None
        self.statements: list[str] = []

    # --- TransactionalStore -----------------------------------------------

    def transaction(self):
        return self

    def __enter__(self):
        if self._transaction is not None:
            raise AssertionError(
                "nested transaction on one AsyncpgStore: the store under test opened a "
                "transaction inside another, which asyncpg would silently make a "
                "savepoint and the advisory lock would no longer mean what it claims"
            )
        self._transaction = self._connection.transaction()
        self._loop.run(self._transaction.start())
        return self

    def __exit__(self, exc_type, exc, tb):
        transaction, self._transaction = self._transaction, None
        # Rollback on any exception, which is what makes the atomicity claim real: a
        # refusal raised mid-reserve must leave no row and must release the advisory lock.
        if exc_type is None:
            self._loop.run(transaction.commit())
        else:
            self._loop.run(transaction.rollback())
        return False

    def execute(
        self, statement: str, parameters: Mapping[str, object]
    ) -> Sequence[Mapping[str, object]]:
        self.statements.append(statement)
        sql, arguments = named_to_positional(statement, parameters)
        rows = self._loop.run(self._connection.fetch(sql, *arguments))
        return [dict(row) for row in rows]

    # --- test helpers ------------------------------------------------------

    def fetch(self, sql: str, *arguments):
        """Read the database directly, bypassing the store under test.

        Every assertion about what is actually stored goes through this rather than through
        the store's own return values, so a store that reported success without writing
        anything would be caught.
        """
        return [
            dict(row) for row in self._loop.run(self._connection.fetch(sql, *arguments))
        ]


def named_to_positional(
    statement: str, parameters: Mapping[str, object]
) -> tuple[str, list[object]]:
    """`:name` → `$1`, via SQLAlchemy's asyncpg dialect rather than string surgery."""
    sa = require_sqlalchemy()
    from sqlalchemy.dialects import postgresql

    compiled = sa.text(statement).compile(dialect=postgresql.asyncpg.dialect())
    return str(compiled), [parameters[name] for name in compiled.positiontup]
