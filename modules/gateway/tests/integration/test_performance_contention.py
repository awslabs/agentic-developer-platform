"""Lock graph regression on a disposable local or configured PostgreSQL test target."""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from secrets import token_urlsafe
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import asyncpg
import psycopg2
import pytest
from psycopg2 import sql
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.budget.settlement import settle_usage
from src.shared.models.base import Base
from src.shared.models.budget import BudgetSettlementReceipt
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.performance_diagnostics import capture_blockers
from src.shared.schemas.auth import TokenContext
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401


@pytest.fixture
async def pg_sessions(request):
    bootstrap_dsn = request.getfixturevalue("pg_url")
    bootstrap_engine = create_async_engine(to_async_url(bootstrap_dsn))
    try:
        async with bootstrap_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await bootstrap_engine.dispose()

    app_role = f"gateway_app_{uuid4().hex[:12]}"
    diagnostic_role = f"gateway_diag_{uuid4().hex[:12]}"
    url = make_url(bootstrap_dsn)
    with psycopg2.connect(bootstrap_dsn) as bootstrap:
        with bootstrap.cursor() as cursor:
            for role, password in ((app_role, token_urlsafe(24)), (diagnostic_role, token_urlsafe(24))):
                cursor.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
                if role == app_role:
                    app_password = password
                else:
                    diagnostic_password = password
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}, {}").format(
                    sql.Identifier(url.database), sql.Identifier(app_role), sql.Identifier(diagnostic_role)
                )
            )
            cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(app_role)))
            cursor.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(app_role)))
            cursor.execute(sql.SQL("GRANT INSERT, UPDATE ON budget_settlement_receipts, budget_usage TO {}").format(sql.Identifier(app_role)))
            cursor.execute(sql.SQL("GRANT pg_read_all_stats TO {}").format(sql.Identifier(diagnostic_role)))

    app_dsn = url.set(username=app_role, password=app_password).render_as_string(hide_password=False)
    diagnostic_dsn = url.set(username=diagnostic_role, password=diagnostic_password).render_as_string(hide_password=False)
    engine = create_async_engine(to_async_url(app_dsn), pool_size=4, max_overflow=0, pool_timeout=1, pool_pre_ping=True)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False), diagnostic_dsn, app_role, diagnostic_role
    finally:
        await engine.dispose()
        with psycopg2.connect(bootstrap_dsn) as bootstrap:
            with bootstrap.cursor() as cursor:
                for role in (app_role, diagnostic_role):
                    cursor.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                    cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


async def test_blocker_graph_captures_settlement_owner_and_unrelated_tenant_progress(pg_sessions, monkeypatch):
    factory, diagnostic_dsn, app_role, diagnostic_role = pg_sessions
    assert app_role != diagnostic_role
    async with factory() as seeded:
        assert (await seeded.scalar(text("SELECT current_user"))) == app_role
        await settle_usage(
            seeded,
            org_id="tenant-a",
            request_id="receipt-a",
            user_id="caller-a",
            cost=Decimal("0"),
            total_tokens=0,
            entities=[],
        )
        await seeded.commit()

    ready = asyncio.Event()
    waiter_pid = None

    async def replay():
        nonlocal waiter_pid
        async with factory() as waiting:
            waiter_pid = (await waiting.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            ready.set()
            return await settle_usage(
                waiting,
                org_id="tenant-a",
                request_id="receipt-a",
                user_id="caller-a",
                cost=Decimal("0"),
                total_tokens=0,
                entities=[],
            )

    async with factory() as owner:
        await owner.execute(select(BudgetSettlementReceipt).where(BudgetSettlementReceipt.request_id == "receipt-a").with_for_update())
        owner_pid = (await owner.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        waiter = asyncio.create_task(replay())
        try:
            await asyncio.wait_for(ready.wait(), 2)
            sample = None
            async with asyncio.timeout(3):
                while True:
                    sample = await capture_blockers(diagnostic_dsn)
                    if sample.status == "captured" and any(row.waiter_pid == waiter_pid for row in sample.blockers):
                        break
                    await asyncio.sleep(0.02)
            blocked = next(row for row in sample.blockers if row.waiter_pid == waiter_pid)
            assert owner_pid in blocked.blocker_pids
            assert blocked.blocker_xact_age_ms is not None
            assert blocked.waiter_xact_age_ms is not None
            assert blocked.query_age_ms >= 0
            diagnostic_connection = await asyncpg.connect(dsn=diagnostic_dsn, server_settings={"default_transaction_read_only": "on"})
            try:
                identity = await diagnostic_connection.fetchrow(
                    "SELECT current_user AS role, current_setting('transaction_read_only') AS read_only, "
                    "has_table_privilege(current_user, 'budget_usage', 'INSERT') AS can_write"
                )
                assert identity["role"] == diagnostic_role
                assert identity["read_only"] == "on"
                assert identity["can_write"] is False
            finally:
                await diagnostic_connection.close()
            with patch("boto3.client", return_value=MagicMock()):
                from src.auth import approval_middleware

            monkeypatch.setattr(approval_middleware, "get_settings", lambda: SimpleNamespace(enforce_org_assignment=True, approval_fail_open=False))
            monkeypatch.setattr(approval_middleware.ApprovalEnforcementMiddleware, "_emit_metric", staticmethod(lambda _: None))
            reached = []
            messages = []

            async def next_handler(scope, receive, send):
                reached.append(scope["path"])

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            middleware = approval_middleware.ApprovalEnforcementMiddleware(next_handler)
            middleware._get_session = lambda: factory()
            denied_context = TokenContext(
                user_id="not-a-member",
                org_id="tenant-b",
                team_id="",
                department_id="",
                account_type="human",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
            scope = {"type": "http", "path": "/v1/chat/completions", "method": "POST", "headers": [], "state": {"token_context": denied_context}}
            await asyncio.wait_for(middleware(scope, receive, send), 1)
            assert messages[0]["status"] == 409
            assert not reached
            async with factory() as verification:
                assert await verification.scalar(select(User.id).where(User.cognito_sub == "not-a-member")) is None
                assert await verification.scalar(select(TenantMembership.user_id)) is None
            async with factory() as healthy:
                await asyncio.wait_for(
                    settle_usage(
                        healthy,
                        org_id="tenant-b",
                        request_id="receipt-b",
                        user_id="caller-b",
                        cost=Decimal("0"),
                        total_tokens=0,
                        entities=[],
                    ),
                    1,
                )
                await healthy.commit()
            assert not waiter.done()
        finally:
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter
            await owner.rollback()


async def test_split_request_pool_bounds_checkout_lock_and_statement_waits(pg_sessions, monkeypatch):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.exc import TimeoutError as PoolTimeout

    from src.shared import database
    from src.shared.config import Settings

    factory, _, _, _ = pg_sessions
    app_dsn = factory.kw["bind"].url.render_as_string(hide_password=False)
    settings = Settings(
        _env_file=None,
        database_url=app_dsn,
        rds_pool_enabled=True,
        rds_split_pool_enabled=True,
        rds_request_pool_size=1,
        rds_accounting_pool_size=1,
        rds_pool_timeout_seconds=1,
        rds_lock_timeout_ms=200,
        rds_statement_timeout_ms=1000,
    )
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    database.reset_engine()
    request_engine = database.get_engine()
    accounting_engine = database.get_accounting_engine()
    try:
        async with database.get_session_factory()() as holding:
            assert await holding.scalar(text("SHOW lock_timeout")) == "200ms"
            assert await holding.scalar(text("SHOW statement_timeout")) == "1s"
            with pytest.raises(PoolTimeout):
                async with database.get_session_factory()() as waiting:
                    await asyncio.wait_for(waiting.execute(text("SELECT 1")), 2)
            async with database.get_accounting_session_factory()() as accounting:
                assert await asyncio.wait_for(accounting.scalar(text("SELECT 1")), 1) == 1
        async with factory() as seeded:
            await settle_usage(
                seeded,
                org_id="tenant-a",
                request_id="receipt-a",
                user_id="caller-a",
                cost=Decimal("0"),
                total_tokens=0,
                entities=[],
            )
            await seeded.commit()
        async with factory() as owner:
            await owner.execute(select(BudgetSettlementReceipt).where(BudgetSettlementReceipt.request_id == "receipt-a").with_for_update())
            async with database.get_session_factory()() as waiting:
                with pytest.raises(DBAPIError) as lock_error:
                    await asyncio.wait_for(
                        waiting.execute(select(BudgetSettlementReceipt).where(BudgetSettlementReceipt.request_id == "receipt-a").with_for_update()),
                        2,
                    )
                assert lock_error.value.orig.sqlstate == "55P03"
            await owner.rollback()
        async with database.get_session_factory()() as statement:
            with pytest.raises(DBAPIError) as statement_error:
                await asyncio.wait_for(statement.execute(text("SELECT pg_sleep(3)")), 2)
            assert statement_error.value.orig.sqlstate == "57014"
        assert request_engine.pool.checkedout() == 0
        assert accounting_engine.pool.checkedout() == 0
    finally:
        await request_engine.dispose()
        await accounting_engine.dispose()
        database.reset_engine()


@pytest.mark.parametrize("cancel_target", ["owner", "waiter"])
async def test_cancelling_blocked_transactions_returns_request_pool_to_baseline(pg_sessions, monkeypatch, cancel_target):
    from src.shared import database
    from src.shared.config import Settings

    factory, diagnostic_dsn, app_role, _ = pg_sessions
    app_dsn = factory.kw["bind"].url.render_as_string(hide_password=False)
    async with factory() as seeded:
        await settle_usage(
            seeded,
            org_id="tenant-a",
            request_id="receipt-a",
            user_id="caller-a",
            cost=Decimal("0"),
            total_tokens=0,
            entities=[],
        )
        await seeded.commit()
    settings = Settings(
        _env_file=None,
        database_url=app_dsn,
        rds_pool_enabled=True,
        rds_split_pool_enabled=True,
        rds_request_pool_size=2,
        rds_accounting_pool_size=1,
        rds_pool_timeout_seconds=1,
        rds_lock_timeout_ms=1000,
        rds_statement_timeout_ms=3000,
    )
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    database.reset_engine()
    request_engine = database.get_engine()
    accounting_engine = database.get_accounting_engine()
    owner_ready = asyncio.Event()
    waiter_ready = asyncio.Event()
    release_owner = asyncio.Event()
    waiter_pid = None

    async def owner_work():
        async with asynccontextmanager(database.get_db)() as session:
            await session.execute(select(BudgetSettlementReceipt).where(BudgetSettlementReceipt.request_id == "receipt-a").with_for_update())
            owner_ready.set()
            await release_owner.wait()

    async def waiter_work():
        nonlocal waiter_pid
        async with asynccontextmanager(database.get_db)() as session:
            waiter_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            waiter_ready.set()
            await session.execute(select(BudgetSettlementReceipt).where(BudgetSettlementReceipt.request_id == "receipt-a").with_for_update())

    owner_task = asyncio.create_task(owner_work())
    waiter_task = None
    try:
        await asyncio.wait_for(owner_ready.wait(), 2)
        waiter_task = asyncio.create_task(waiter_work())
        await asyncio.wait_for(waiter_ready.wait(), 2)
        async with asyncio.timeout(2):
            while True:
                graph = await capture_blockers(diagnostic_dsn)
                if graph.status == "captured" and any(row.waiter_pid == waiter_pid for row in graph.blockers):
                    break
                await asyncio.sleep(0.02)
        async with database.get_accounting_session_factory()() as accounting:
            assert await asyncio.wait_for(accounting.scalar(text("SELECT 1")), 1) == 1
        if cancel_target == "owner":
            owner_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner_task
            await asyncio.wait_for(waiter_task, 2)
        else:
            waiter_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter_task
            release_owner.set()
            await asyncio.wait_for(owner_task, 2)
        async with asyncio.timeout(2):
            while request_engine.pool.checkedout():
                await asyncio.sleep(0.02)
        assert request_engine.pool.checkedout() == 0
        assert accounting_engine.pool.checkedout() == 0
        observer = await asyncpg.connect(dsn=diagnostic_dsn, server_settings={"default_transaction_read_only": "on"})
        try:
            idle = await observer.fetchval("SELECT count(*) FROM pg_stat_activity WHERE usename = $1 AND state LIKE 'idle in transaction%'", app_role)
            assert idle == 0
        finally:
            await observer.close()
        async with database.get_session_factory()() as healthy:
            assert await asyncio.wait_for(healthy.scalar(text("SELECT 1")), 1) == 1
    finally:
        release_owner.set()
        for task in (owner_task, waiter_task):
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        await request_engine.dispose()
        await accounting_engine.dispose()
        database.reset_engine()


async def test_terminated_postgres_request_connection_is_replaced_without_pool_leak(pg_sessions, monkeypatch, request):
    from sqlalchemy.exc import DBAPIError

    from src.shared import database
    from src.shared.config import Settings

    factory, diagnostic_dsn, app_role, _ = pg_sessions
    bootstrap_dsn = request.getfixturevalue("pg_url")
    app_dsn = factory.kw["bind"].url.render_as_string(hide_password=False)
    settings = Settings(
        _env_file=None,
        database_url=app_dsn,
        rds_pool_enabled=True,
        rds_split_pool_enabled=True,
        rds_request_pool_size=1,
        rds_accounting_pool_size=1,
        rds_pool_timeout_seconds=1,
        rds_lock_timeout_ms=200,
        rds_statement_timeout_ms=1000,
    )
    monkeypatch.setattr(database, "get_settings", lambda: settings)
    database.reset_engine()
    request_engine = database.get_engine()
    accounting_engine = database.get_accounting_engine()

    try:
        with pytest.raises((DBAPIError, asyncpg.exceptions.InternalClientError)) as fault:
            async with asynccontextmanager(database.get_db)() as session:
                backend_pid = await session.scalar(text("SELECT pg_backend_pid()"))
                await asyncio.to_thread(_terminate_backend, bootstrap_dsn, backend_pid)
                await asyncio.wait_for(session.execute(text("SELECT 1")), 2)
        if isinstance(fault.value, DBAPIError):
            assert fault.value.connection_invalidated is True
        async with asyncio.timeout(2):
            while request_engine.pool.checkedout():
                await asyncio.sleep(0.02)
        assert request_engine.pool.checkedout() == 0
        async with database.get_session_factory()() as healthy:
            replacement_pid = await asyncio.wait_for(healthy.scalar(text("SELECT pg_backend_pid()")), 1)
            assert replacement_pid != backend_pid
            assert await healthy.scalar(text("SELECT current_user")) == app_role
        async with database.get_accounting_session_factory()() as accounting:
            assert await asyncio.wait_for(accounting.scalar(text("SELECT 1")), 1) == 1
        assert accounting_engine.pool.checkedout() == 0
        observer = await asyncpg.connect(dsn=diagnostic_dsn, server_settings={"default_transaction_read_only": "on"})
        try:
            assert (
                await observer.fetchval("SELECT count(*) FROM pg_stat_activity WHERE usename = $1 AND state LIKE 'idle in transaction%'", app_role)
                == 0
            )
        finally:
            await observer.close()
    finally:
        await request_engine.dispose()
        await accounting_engine.dispose()
        database.reset_engine()


async def test_lost_commit_acknowledgement_replay_does_not_double_debit(pg_sessions):
    from src.shared.models.budget import BudgetUsage

    factory, _, _, _ = pg_sessions
    original_args = {
        "org_id": "tenant-a",
        "request_id": "receipt-a",
        "user_id": "caller-a",
        "cost": Decimal("1.000000"),
        "total_tokens": 10,
        "entities": [("org", "tenant-a")],
    }
    async with factory() as writer:
        assert await settle_usage(writer, **original_args) is True
        committed = writer.commit

        async def committed_without_acknowledgement():
            await committed()
            raise ConnectionError("test-only commit acknowledgement lost")

        with patch.object(writer, "commit", committed_without_acknowledgement):
            with pytest.raises(ConnectionError):
                await writer.commit()
    async with factory() as retry:
        assert await settle_usage(retry, **original_args) is False
        await retry.commit()
        with pytest.raises(ValueError, match="conflicting settlement replay"):
            await settle_usage(retry, **{**original_args, "cost": Decimal("2.000000")})
        await retry.rollback()
    async with factory() as verification:
        receipt_count = await verification.scalar(select(func.count()).select_from(BudgetSettlementReceipt))
        usage = await verification.scalar(select(BudgetUsage).where(BudgetUsage.org_id == "tenant-a"))
        assert receipt_count == 1
        assert usage.request_count == 1
        assert usage.total_cost_usd == Decimal("1.000000")


def _terminate_backend(bootstrap_dsn, pid):
    with psycopg2.connect(bootstrap_dsn) as bootstrap:
        with bootstrap.cursor() as cursor:
            cursor.execute("SELECT pg_terminate_backend(%s)", (pid,))
            assert cursor.fetchone()[0] is True


async def test_approval_database_disconnect_is_retryable_503_not_policy_denial(pg_sessions, monkeypatch, request):
    factory, _, app_role, _ = pg_sessions
    bootstrap_dsn = request.getfixturevalue("pg_url")
    with patch("boto3.client", return_value=MagicMock()):
        from src.auth import approval_middleware

    monkeypatch.setattr(approval_middleware, "get_settings", lambda: SimpleNamespace(enforce_org_assignment=True, approval_fail_open=False))
    monkeypatch.setattr(approval_middleware.ApprovalEnforcementMiddleware, "_emit_metric", staticmethod(lambda _: None))
    reached = []
    messages = []

    async def handler(scope, receive, send):
        reached.append(scope["path"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    @asynccontextmanager
    async def failed_connection():
        async with factory() as session:
            backend_pid = await session.scalar(text("SELECT pg_backend_pid()"))
            await asyncio.to_thread(_terminate_backend, bootstrap_dsn, backend_pid)
            yield session

    middleware = approval_middleware.ApprovalEnforcementMiddleware(handler)
    middleware._get_session = failed_connection
    context = TokenContext(
        user_id="test-subject",
        org_id="tenant-a",
        team_id="",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    scope = {"type": "http", "path": "/v1/chat/completions", "method": "POST", "headers": [], "state": {"token_context": context}}
    await asyncio.wait_for(middleware(scope, receive, send), 2)
    assert messages[0]["status"] == 503
    assert (b"retry-after", b"2") in messages[0]["headers"]
    assert not reached
    async with factory() as healthy:
        assert await healthy.scalar(text("SELECT current_user")) == app_role
    assert factory.kw["bind"].pool.checkedout() == 0
    messages.clear()
    middleware._get_session = factory
    await asyncio.wait_for(middleware(scope, receive, send), 2)
    assert messages[0]["status"] == 409
    assert not reached
    assert factory.kw["bind"].pool.checkedout() == 0
