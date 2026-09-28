"""Real PostgreSQL concurrency tests for PMM-02 register/link race handling.

These tests exercise ``register_service_principal()`` and ``link_alias()``
through independent asyncpg sessions against a real PostgreSQL 16 server,
proving:

- One winner and one stable ``PreferenceRejectedError("alias_already_active")``
  (not an unhandled 500) when two sessions race to register/link the same alias.
- Exactly one active alias row in the database after the race.
- No orphan principal row when the losing ``register_service_principal`` rolls back.
- The losing caller's outer transaction remains usable (can commit unrelated work).
- An unrelated IntegrityError (e.g. CHECK constraint violation) propagates as
  ``IntegrityError``, not as ``alias_already_active``.
- The ``_extract_constraint_name`` function correctly reads ``constraint_name``
  from asyncpg's ``UniqueViolationError`` (which has no ``.diag`` attribute).

**Why this file exists separately from the SQLite tests.**

SQLite's ``StaticPool`` shares a single connection, making concurrent sessions
unobservable. The file-backed ``concurrent_engine`` fixture provides real
interleaving on SQLite, but it does not exercise the production driver (asyncpg)
or the asyncpg-specific constraint-name extraction path. These tests exercise
the actual ``postgresql+asyncpg`` driver against ``pgserver``'s embedded
PostgreSQL 16 so that the ``_extract_constraint_name`` asyncpg path and the
SAVEPOINT recovery are proven against the real engine, not just plausible.

Skips automatically where ``pgserver`` is unavailable (Python 3.13).
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.admin.persona_models import service
from src.shared.models.base import Base, new_uuid
from src.shared.models.persona_models import ServicePrincipal, ServicePrincipalAlias

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key")
os.environ.setdefault("BG_TOKEN_SECRET_KEY", "test-secret-key")
os.environ.setdefault("REDIS_URL", "")

TEST_ORG = "org-pg-concurrent"
ADMIN_USER = "admin-pg-001"


class _TwoPartyBarrier:
    """Release both racers only after both alias preflight reads complete."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._arrivals = 0

    async def wait(self) -> None:
        async with self._condition:
            self._arrivals += 1
            if self._arrivals == 2:
                self._condition.notify_all()
                return
            await self._condition.wait_for(lambda: self._arrivals == 2)


class _BarrierSession(AsyncSession):
    """Pause once after this session's ServicePrincipalAlias preflight read."""

    async def scalar(self, statement, *args, **kwargs):
        result = await super().scalar(statement, *args, **kwargs)
        barrier = self.info.get("alias_preflight_barrier")
        entities = [item.get("entity") for item in getattr(statement, "column_descriptions", ())]
        if barrier is not None and not self.info.get("alias_preflight_waited") and ServicePrincipalAlias in entities:
            self.info["alias_preflight_waited"] = True
            await barrier.wait()
        return result


def _require_pgserver():
    """Skip the test cleanly when pgserver is not available."""
    try:
        import pgserver
    except ImportError:
        pytest.skip("Real PostgreSQL concurrency tests require pgserver (Python <= 3.12); see tests/migrations/README-postgres.md")
    return pgserver


def _require_psycopg2():
    try:
        import psycopg2
    except ImportError:
        pytest.skip("Real PostgreSQL concurrency tests require psycopg2")
    return psycopg2


def _ensure_pgcrypto_shim(pgserver):
    """Same shim as conftest_postgres.py — pgcrypto is needed for gen_random_uuid()."""
    import inspect
    from pathlib import Path

    extension_dir = Path(inspect.getfile(pgserver)).parent / "pginstall/share/postgresql/extension"
    if not extension_dir.is_dir():
        pytest.skip(f"pgserver extension directory not found at {extension_dir}")
    control = extension_dir / "pgcrypto.control"
    if control.exists():
        return
    try:
        control.write_text("comment = 'pgcrypto shim (test harness)'\ndefault_version = '1.3'\nrelocatable = true\n")
        (extension_dir / "pgcrypto--1.3.sql").write_text("-- Test-harness shim\nDO $$ BEGIN PERFORM gen_random_uuid(); END $$;\n")
    except OSError as exc:
        pytest.skip(f"cannot install pgcrypto test shim: {exc}")


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def pg_server(tmp_path_factory):
    pgserver = _require_pgserver()
    _require_psycopg2()
    _ensure_pgcrypto_shim(pgserver)
    data_dir = tmp_path_factory.mktemp("pgdata-pmm02")
    server = pgserver.get_server(str(data_dir))
    try:
        yield server
    finally:
        server.cleanup()


@pytest.fixture
def pg_async_url(pg_server):
    """A fresh PostgreSQL database per test with the asyncpg driver."""
    import psycopg2

    name = f"t{uuid.uuid4().hex[:16]}"
    admin_url = pg_server.get_uri()

    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
    finally:
        conn.close()

    # Rewrite the URL to use the asyncpg driver
    raw_url = pg_server.get_uri(database=name)
    for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
        if raw_url.startswith(prefix):
            async_url = "postgresql+asyncpg://" + raw_url[len(prefix) :]
            break
    else:
        async_url = raw_url

    yield async_url

    conn = psycopg2.connect(admin_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
    finally:
        conn.close()


@pytest.fixture
async def pg_engine(pg_async_url):
    """An asyncpg engine with NullPool (each session gets its own connection)."""
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


# ── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_register_one_winner_one_conflict(pg_engine):
    """Two sessions race to register the same alias — one wins, one gets alias_already_active.

    Proves:
    - One winner, one PreferenceRejectedError with reason="alias_already_active"
    - Exactly one active alias row
    - No orphan principal (the loser's principal is rolled back with the savepoint)
    - The losing caller's outer transaction can still commit a sentinel row
    """
    factory = async_sessionmaker(pg_engine, class_=_BarrierSession, expire_on_commit=False)
    barrier = _TwoPartyBarrier()

    async def attempt_register(label: str) -> tuple[str, str | None, Exception | None]:
        """Run register_service_principal and return (label, canonical_id, error)."""
        async with factory() as session:
            session.info["alias_preflight_barrier"] = barrier
            try:
                principal, alias = await service.register_service_principal(
                    session,
                    org_id=TEST_ORG,
                    display_name=f"Racer {label}",
                    alias_source="agent_registry",
                    alias_id="contested-agent",
                    approved_by=ADMIN_USER,
                )
                await session.commit()
                return (label, principal.canonical_service_principal_id, None)
            except service.PreferenceRejectedError as exc:
                # Prove the outer transaction is still usable: insert a sentinel
                sentinel = ServicePrincipal(
                    canonical_service_principal_id=f"sentinel-{label}",
                    org_id=TEST_ORG,
                    display_name=f"Sentinel {label}",
                    status="active",
                    approved_by=ADMIN_USER,
                )
                session.add(sentinel)
                await session.commit()
                return (label, None, exc)
            except Exception as exc:
                return (label, None, exc)

    results = await asyncio.wait_for(
        asyncio.gather(attempt_register("A"), attempt_register("B")),
        timeout=20,
    )

    winners = [r for r in results if r[1] is not None]
    losers = [r for r in results if r[2] is not None]

    assert len(winners) == 1, f"Expected exactly one winner, got {len(winners)}"
    assert len(losers) == 1, f"Expected exactly one loser, got {len(losers)}"

    loser_label, _, loser_exc = losers[0]
    assert isinstance(loser_exc, service.PreferenceRejectedError), (
        f"Loser got {type(loser_exc).__name__} instead of PreferenceRejectedError: {loser_exc}"
    )
    assert loser_exc.reason == "alias_already_active"
    assert isinstance(loser_exc.__cause__, IntegrityError)
    assert service._extract_constraint_name(loser_exc.__cause__) == "uq_spa_active_alias"

    # Exactly one active alias
    async with factory() as session:
        alias_count = await session.scalar(
            select(func.count())
            .select_from(ServicePrincipalAlias)
            .where(
                ServicePrincipalAlias.org_id == TEST_ORG,
                ServicePrincipalAlias.alias_source == "agent_registry",
                ServicePrincipalAlias.alias_id == "contested-agent",
                ServicePrincipalAlias.is_active == True,  # noqa: E712
            )
        )
    assert alias_count == 1, f"Expected exactly one active alias, got {alias_count}"

    # No orphan principal: count principals minus the sentinel
    async with factory() as session:
        principal_count = await session.scalar(
            select(func.count())
            .select_from(ServicePrincipal)
            .where(
                ServicePrincipal.org_id == TEST_ORG,
                ServicePrincipal.display_name.like("Racer %"),
            )
        )
    assert principal_count == 1, f"Expected exactly one Racer principal (no orphan), got {principal_count}"

    # The sentinel from the loser committed, proving outer transaction is usable
    async with factory() as session:
        sentinel = await session.scalar(
            select(ServicePrincipal).where(
                ServicePrincipal.canonical_service_principal_id == f"sentinel-{loser_label}",
            )
        )
    assert sentinel is not None, "Loser's sentinel was not committed — outer transaction was poisoned"


@pytest.mark.asyncio
async def test_concurrent_link_one_winner_one_conflict(pg_engine):
    """Two sessions race to link the same alias to different principals — one wins, one 409.

    Proves:
    - One winner, one PreferenceRejectedError with reason="alias_already_active"
    - Exactly one active alias row
    - The losing outer transaction can still commit
    """
    factory = async_sessionmaker(pg_engine, class_=_BarrierSession, expire_on_commit=False)
    barrier = _TwoPartyBarrier()

    # Seed two principals for the two racers to link to
    async with factory() as session:
        for i in range(2):
            session.add(
                ServicePrincipal(
                    canonical_service_principal_id=f"link-target-{i}",
                    org_id=TEST_ORG,
                    display_name=f"Link target {i}",
                    status="active",
                    approved_by=ADMIN_USER,
                )
            )
        await session.commit()

    async def attempt_link(target_index: int) -> tuple[int, str | None, Exception | None]:
        async with factory() as session:
            session.info["alias_preflight_barrier"] = barrier
            try:
                alias = await service.link_alias(
                    session,
                    canonical_id=f"link-target-{target_index}",
                    org_id=TEST_ORG,
                    alias_source="cognito_m2m",
                    alias_id="contested-link-alias",
                    registered_by=ADMIN_USER,
                )
                await session.commit()
                return (target_index, alias.id, None)
            except service.PreferenceRejectedError as exc:
                # Prove outer transaction usable
                sentinel = ServicePrincipal(
                    canonical_service_principal_id=f"link-sentinel-{target_index}",
                    org_id=TEST_ORG,
                    display_name=f"Link sentinel {target_index}",
                    status="active",
                    approved_by=ADMIN_USER,
                )
                session.add(sentinel)
                await session.commit()
                return (target_index, None, exc)
            except Exception as exc:
                return (target_index, None, exc)

    results = await asyncio.wait_for(
        asyncio.gather(attempt_link(0), attempt_link(1)),
        timeout=20,
    )

    winners = [r for r in results if r[1] is not None]
    losers = [r for r in results if r[2] is not None]

    assert len(winners) == 1, f"Expected exactly one winner, got {len(winners)}"
    assert len(losers) == 1, f"Expected exactly one loser, got {len(losers)}"

    _, _, loser_exc = losers[0]
    assert isinstance(loser_exc, service.PreferenceRejectedError)
    assert loser_exc.reason == "alias_already_active"
    assert isinstance(loser_exc.__cause__, IntegrityError)
    assert service._extract_constraint_name(loser_exc.__cause__) == "uq_spa_active_alias"

    # Exactly one active alias
    async with factory() as session:
        alias_count = await session.scalar(
            select(func.count())
            .select_from(ServicePrincipalAlias)
            .where(
                ServicePrincipalAlias.org_id == TEST_ORG,
                ServicePrincipalAlias.alias_source == "cognito_m2m",
                ServicePrincipalAlias.alias_id == "contested-link-alias",
                ServicePrincipalAlias.is_active == True,  # noqa: E712
            )
        )
    assert alias_count == 1, f"Expected exactly one active alias, got {alias_count}"

    # Loser sentinel committed
    loser_idx = losers[0][0]
    async with factory() as session:
        sentinel = await session.scalar(
            select(ServicePrincipal).where(
                ServicePrincipal.canonical_service_principal_id == f"link-sentinel-{loser_idx}",
            )
        )
    assert sentinel is not None, "Loser's sentinel was not committed — outer transaction was poisoned"


@pytest.mark.asyncio
async def test_unrelated_constraint_violation_propagates(pg_engine):
    """A CHECK constraint violation (not uq_spa_active_alias) must propagate as IntegrityError.

    The _is_active_alias_uniqueness_violation function must return False for
    non-uniqueness violations so they are re-raised, not reported as alias conflicts.
    """
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)

    # Exercise the production registration function with an invalid source. Its
    # savepoint catches IntegrityError only to classify the named alias race;
    # this unrelated CHECK violation must propagate unchanged.
    async with factory() as session:
        with pytest.raises(IntegrityError) as exc_info:
            await service.register_service_principal(
                session,
                org_id=TEST_ORG,
                display_name="Invalid source",
                alias_source="invalid_source_not_in_check",
                alias_id="check-alias",
                approved_by=ADMIN_USER,
            )

        assert service._extract_constraint_name(exc_info.value) == "ck_spa_alias_source"
        assert not service._is_active_alias_uniqueness_violation(exc_info.value), (
            "_is_active_alias_uniqueness_violation returned True for a CHECK violation — "
            "this would cause the CHECK failure to be reported as alias_already_active"
        )

        # The production savepoint must also leave the outer transaction usable.
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="check-sentinel",
                org_id=TEST_ORG,
                display_name="CHECK sentinel",
                status="active",
                approved_by=ADMIN_USER,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_asyncpg_constraint_name_extraction(pg_engine):
    """Prove _extract_constraint_name reads the correct name from asyncpg exceptions.

    This is the critical path: in production (asyncpg driver), the constraint
    name is directly on the exception object, not behind .diag. If extraction
    fails, every concurrent race becomes an unhandled 500.
    """
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)

    # Seed a principal and alias
    async with factory() as session:
        session.add(
            ServicePrincipal(
                canonical_service_principal_id="extract-test-principal",
                org_id=TEST_ORG,
                display_name="Extraction test",
                status="active",
                approved_by=ADMIN_USER,
            )
        )
        session.add(
            ServicePrincipalAlias(
                id=new_uuid(),
                canonical_service_principal_id="extract-test-principal",
                org_id=TEST_ORG,
                alias_source="agent_registry",
                alias_id="extract-agent",
                is_active=True,
                registered_by=ADMIN_USER,
            )
        )
        await session.commit()

    # Force a duplicate to trigger the constraint
    async with factory() as session:
        dup = ServicePrincipalAlias(
            id=new_uuid(),
            canonical_service_principal_id="extract-test-principal",
            org_id=TEST_ORG,
            alias_source="agent_registry",
            alias_id="extract-agent",
            is_active=True,
            registered_by=ADMIN_USER,
        )
        session.add(dup)
        try:
            await session.flush()
            pytest.fail("Expected IntegrityError for duplicate alias")
        except IntegrityError as exc:
            constraint = service._extract_constraint_name(exc)
            assert constraint == "uq_spa_active_alias", (
                f"_extract_constraint_name returned '{constraint}' instead of 'uq_spa_active_alias' — "
                "constraint-name extraction is broken for the asyncpg driver"
            )
            assert service._is_active_alias_uniqueness_violation(exc), (
                "_is_active_alias_uniqueness_violation returned False despite the constraint "
                "being uq_spa_active_alias — the race handler is broken for asyncpg"
            )
