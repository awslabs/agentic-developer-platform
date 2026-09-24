"""The database seam between this app's SQLAlchemy layer and ``harness_jobs``.

Issue #5535 (Superplane W6), EPIC #4910.

``harness_jobs`` reads no DSN and holds no credential — deliberately, so that it
cannot become a second composition root (``harness_jobs/__init__.py:54``). It is
handed a ``connect`` callable instead: a zero-argument callable returning an async
context manager that yields something satisfying its ``store.Connection``
Protocol. This module is that callable.

## Why a dedicated asyncpg pool and not this app's SQLAlchemy engine

The tempting shortcut is to reach through SQLAlchemy:
``(await conn.get_raw_connection()).driver_connection`` **is** a genuine
``asyncpg.Connection`` and does satisfy the whole Protocol — measured, not
assumed. It is still the wrong seam, for three reasons that are properties of the
harness's contract rather than matters of taste:

1. **It requires an idle connection.** ``admission._admission_ownership`` raises
   ``ContractViolation("admission and recovery require an idle database
   connection")`` when ``connection.is_in_transaction()``. A connection borrowed
   from a SQLAlchemy session is normally inside that session's transaction, so
   admission would refuse — and refuse *intermittently*, depending on whether
   the caller had already emitted a statement.
2. **It holds a session-level advisory lock across a context.** That lock is
   released when the *session* ends, so the connection must stay exclusively
   owned by the harness for the duration. Handing out a pooled connection that
   SQLAlchemy may also use, reset, or return to the pool mid-operation breaks the
   mutual exclusion admission depends on for its recovery interlock.
3. **It opens its own transactions** (``async with connection.transaction()``).
   Nesting those inside a SQLAlchemy-managed transaction would put commit
   boundaries the harness reasons about under the control of a session it cannot
   see.

So the harness gets its own pool. The cost is a second set of connections; the
thing bought is that the harness's transaction and locking assumptions are true
rather than incidentally true.

## Why the transport configuration is imported rather than restated

``connect_args`` in ``app/schema_boundary.py`` already answers "how is this
transport secured" for the API sessions, the Alembic chain and the one-shot jobs,
and its docstring records why that answer is a single function: guarding some
entry points and not others leaves an equivalent gap open while the finding reads
as closed. A new pool is exactly such an entry point.

It also happens to be directly usable: ``connect_args`` returns **native asyncpg
keyword arguments** (``ssl=<SSLContext>``, ``server_settings={"search_path":
...}``), because SQLAlchemy's asyncpg dialect passes them straight through. So
this pool inherits mandatory certificate *and hostname* verification, the
fail-closed behaviour when no CA is configured, and the one narrowly-guarded
local exception — without restating any of it. A hand-written ``ssl=`` here would
be a second transport posture that drifts from the first one silently, which is
the defect #5676 removed.

## What this module does not do

It does not apply the harness schema. ``harness_jobs.schema.apply`` is DDL, and
DDL belongs to an authorized installer, not to a request-serving process that
happens to boot first. ``ensure_ready`` therefore *checks* the version and
refuses when it does not match, which is the same asymmetry
``schema_boundary.transport_connect_args`` keeps: absent configuration is a
refusal to start, not a silent self-upgrade. The harness's tables are
``harness_*`` and are owned by ``harness_jobs.schema``; they are deliberately not
in this component's Alembic chain (``harness_jobs/schema.py:8``: putting them
there would mean "the domain API has implemented shared jobs").
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

logger = logging.getLogger(__name__)

# Small by design. This pool serves operation admission and inventory reads, not
# the request-path ORM traffic, and every connection it holds is one the harness
# may keep exclusively for the length of an advisory-locked admission. A large
# pool here would mostly buy idle connections against the database's own limit,
# which the API sessions (pool_size=5, max_overflow=10) already draw on.
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 4

# A boot-time connection must not hang the lifespan. The capability preflight runs
# with `--network=none`, where connecting fails fast; this bounds the case where a
# reachable host accepts the TCP connection and then never completes the
# handshake, which is indistinguishable from a hang without a timeout.
CONNECT_TIMEOUT_SECONDS = 10.0


class HarnessDatabaseUnavailable(RuntimeError):
    """The harness operation store could not be reached or is the wrong version.

    A ``RuntimeError`` rather than a ``PermissionError``: nothing about a caller's
    request is wrong. Callers translate it to the unavailable half of their port's
    contract, never to a refusal — a refusal would invite a retry of something
    that is not the caller's to fix, and would report a configuration fault as the
    caller's lack of permission.
    """


def _asyncpg_dsn(database_url: str) -> str:
    """The SQLAlchemy URL as a DSN asyncpg accepts.

    Only the driver prefix differs; asyncpg rejects the ``+asyncpg`` dialect
    marker SQLAlchemy requires. Everything else — host, socket directory, query
    parameters — is left exactly as configured, so this cannot retarget the
    connection.
    """
    for prefix in ("postgresql+asyncpg://", "postgresql://"):
        if database_url.startswith(prefix):
            return "postgresql://" + database_url[len(prefix) :]
    # Not silently accepted: an unrecognized driver would otherwise be handed to
    # asyncpg, which would report a parse failure whose message can contain the
    # DSN — and a DSN can contain a password.
    raise HarnessDatabaseUnavailable(
        "the harness operation store requires a PostgreSQL database URL"
    )


class HarnessConnections:
    """Owns one asyncpg pool and hands out connections the harness can use.

    Constructed but not connected. ``open()`` is a separate, awaited step so that
    composition — which is synchronous and runs in contexts with no event loop and
    no database, such as the packaged capability preflight — can build this object
    without incurring a connection. A constructor that connected would make the
    preflight's ``--network=none`` run fail at import rather than report a
    capability.
    """

    def __init__(self, dsn: str, connect_args: dict[str, Any]) -> None:
        self._dsn = dsn
        self._connect_args = connect_args
        self._pool: Any = None

    @property
    def opened(self) -> bool:
        return self._pool is not None

    async def open(self) -> None:
        """Create the pool. Idempotent, so a second startup is not a second pool."""
        if self._pool is not None:
            return
        import asyncpg

        try:
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=POOL_MIN_SIZE,
                max_size=POOL_MAX_SIZE,
                timeout=CONNECT_TIMEOUT_SECONDS,
                **self._connect_args,
            )
        except Exception as error:
            # Never the original message and never the DSN: asyncpg's connection
            # errors quote the target, and the target carries a credential.
            raise HarnessDatabaseUnavailable(
                "the harness operation store could not be reached"
            ) from _redacted(error)

    async def aclose(self) -> None:
        """Close the pool. Idempotent, and never raises out of a shutdown path."""
        pool, self._pool = self._pool, None
        if pool is None:
            return
        try:
            await pool.close()
        except Exception:
            logger.warning(
                "the harness database pool did not close cleanly", exc_info=False
            )

    def connect(self) -> AbstractAsyncContextManager[Any]:
        """The ``connect`` callable ``harness_jobs`` is composed with.

        Yields a pooled ``asyncpg.Connection``, which satisfies the harness's
        ``store.Connection`` Protocol in full — ``execute``, ``fetchrow``,
        ``fetch``, ``fetchval``, ``transaction`` and ``is_in_transaction``. The
        connection is exclusively the harness's for the length of the context,
        which is what its advisory-lock interlock requires, and it is returned to
        the pool idle, which is what its next admission requires.
        """

        @asynccontextmanager
        async def _acquire() -> AsyncIterator[Any]:
            if self._pool is None:
                raise HarnessDatabaseUnavailable(
                    "the harness operation store is not connected"
                )
            async with self._pool.acquire() as connection:
                yield connection

        return _acquire()

    async def ensure_ready(self) -> None:
        """Refuse unless the harness schema present is the version it expects.

        Checks; never applies. A serving process that upgraded its own shared
        schema would be doing an installer's job with a request-path credential,
        and would do it from however many replicas happened to boot first.

        ``SchemaMismatch`` is translated rather than propagated so that callers
        have one exception to handle for "the store cannot answer", and so that a
        version number — which describes deployment state — does not reach a
        tenant-facing response.
        """
        from harness_jobs.schema import SchemaMismatch, check_schema_version

        async with self.connect() as connection:
            try:
                await check_schema_version(connection)
            except SchemaMismatch as mismatch:
                logger.error(
                    "the harness operation store schema is not the expected "
                    "version; run the authorized installer's migration step"
                )
                raise HarnessDatabaseUnavailable(
                    "the harness operation store schema is not the expected version"
                ) from mismatch


def _redacted(error: BaseException) -> BaseException | None:
    """Keep the exception *type* for diagnosis and drop its message.

    A connection failure's ``str()`` routinely contains the host, the user and —
    for a URL-form DSN — the password. Chaining the original would put it in every
    traceback and every log aggregator. The type alone distinguishes a DNS failure
    from a TLS failure from a timeout, which is what a diagnosis needs.
    """
    return type(error)(f"{type(error).__name__} (details redacted)")


def build_harness_connections(settings: Any) -> HarnessConnections | None:
    """Build the harness's database seam, or ``None`` when unconfigured.

    ``None`` rather than a raise, and ``None`` rather than a stub: an unconfigured
    deployment composes no adapter, and the ports then answer their contract's
    unavailable outcome. A stub that refused everything would report a
    configuration gap as a per-request denial, and a stub that accepted anything
    would be a bypass.
    """
    database_url = getattr(settings, "database_url", "") or ""
    if not database_url.strip():
        return None

    from app.schema_boundary import connect_args

    schema = getattr(settings, "superplane_db_schema", "") or ""
    return HarnessConnections(
        _asyncpg_dsn(database_url), connect_args(schema, database_url)
    )


ConnectCallable = Callable[[], AbstractAsyncContextManager[Any]]
