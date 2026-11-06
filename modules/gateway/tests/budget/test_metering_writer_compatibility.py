"""Legacy gateway and tracker writers share metering gate order and debit authority."""

import asyncio
import importlib
from datetime import datetime
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.budget.service import BudgetService
from src.budget.settlement import settle_priced_usage, settle_usage
from src.shared.schemas.budget import CostRecordRequest, EntityType
from tests.budget.test_metering_person_scopes import activate_person_scopes, person_key, seed_person
from tests.budget.test_metering_scope_admission import admission_for, context
from tests.budget.test_metering_scope_admission import journal as base_journal
from tests.budget.test_metering_scope_admission import metering_db as base_metering_db
from tests.budget.test_metering_settlement_receipts import complete
from tests.migrations.conftest_postgres import to_async_url

pytest_plugins = ("tests.migrations.conftest_postgres",)
support = importlib.import_module("tests.lambda.test_budget_usage_tracker")


@pytest.fixture
def metering_db(pg_head_url):
    return base_metering_db.__wrapped__(pg_head_url)


@pytest.fixture
def journal():
    yield from base_journal.__wrapped__()


def deliver(pg_url, *, request_id="legacy", org_id="tenant-1", user_id="owner-1", account_type="service"):
    log = support._chat_log_4300(request_id=request_id, org_id=org_id, user_id=user_id, account_type=account_type)
    with psycopg2.connect(pg_url) as connection:
        support.load_handler("budget-usage-tracker").process_chat_log(connection, log, support._bundled_rate_source())
    return log


def ledger_rows(pg_url):
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT request_id, cost_usd FROM budget_settlement_receipts ORDER BY request_id")
        receipts = cursor.fetchall()
        cursor.execute("SELECT entity_type, period_type, total_cost_usd, request_count FROM budget_usage ORDER BY entity_type, period_type")
        debits = cursor.fetchall()
        cursor.execute("SELECT request_id, cost_usd FROM usage_logs ORDER BY request_id")
        usages = cursor.fetchall()
    return receipts, debits, usages


async def test_metered_receipt_excludes_tracker_even_before_sql_settlement(metering_db, journal):
    admission, engine = admission_for(metering_db, journal)
    try:
        attempt, final, decision = await complete(admission, context(), "single-authority")
        with pytest.raises(ValueError, match="transcript conflicts"):
            await asyncio.to_thread(deliver, metering_db, request_id="single-authority", user_id="different-owner")
        await asyncio.to_thread(deliver, metering_db, request_id="single-authority")
        assert ledger_rows(metering_db) == ([], [], [])
        assert await admission.settle_receipt(attempt, final) is True
        before = ledger_rows(metering_db)
        await asyncio.to_thread(deliver, metering_db, request_id="single-authority")
        assert ledger_rows(metering_db) == before
        receipts, debits, usages = before
        assert receipts == [("single-authority", decision.ledger_cost)]
        assert len(debits) == 9 and all(cost == decision.ledger_cost and count == 1 for _, _, cost, count in debits)
        assert usages == [("single-authority", decision.ledger_cost)]
    finally:
        await engine.dispose()


async def test_active_gate_rejects_legacy_tracker_and_gateway_without_debits(metering_db, journal):
    handler = support.load_handler("budget-usage-tracker")
    with psycopg2.connect(metering_db) as connection:
        with pytest.raises(ValueError, match="active metering gate"):
            handler.process_chat_log(
                connection, support._chat_log_4300(org_id="tenant-1", user_id="owner-1", account_type="service"), support._bundled_rate_source()
            )
        connection.rollback()
        with pytest.raises(ValueError, match="active metering gate"):
            handler.process_chat_log(
                connection,
                support._chat_log_4300(org_id="tenant-1", user_id="owner-1", account_type="service", request_id=None),
                support._bundled_rate_source(),
            )
        connection.rollback()
    admission, engine = admission_for(metering_db, journal)
    try:
        attempt, _, decision = await complete(admission, context(), "metered-only")
        async with admission._session_factory() as session:
            with pytest.raises(ValueError, match="active metering gate"):
                await settle_priced_usage(session, context=context(), request_id=attempt.receipt.request_id, decision=decision)
            await session.rollback()
            service = BudgetService(session)
            with pytest.raises(ValueError, match="active metering gate"):
                await service.record_cost(
                    CostRecordRequest(
                        entity_type=EntityType.USER,
                        entity_id="owner-1",
                        model_name="anthropic.claude-3-5-sonnet-20241022-v2:0",
                        tokens_in=1,
                        tokens_out=2,
                        request_cost_usd=Decimal("0.1"),
                    ),
                    "tenant-1",
                )
            await session.rollback()
            with pytest.raises(ValueError, match="active metering gate"):
                await service.record_usage(context(), tokens_in=1, tokens_out=2, model="anthropic.claude-3-5-sonnet-20241022-v2:0")
            await session.rollback()
        assert ledger_rows(metering_db) == ([], [], [])
    finally:
        await engine.dispose()


async def test_legacy_tracker_and_gateway_replay_share_one_receipt(metering_db):
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE metering_scope_gates SET activation='legacy' WHERE tenant_id='tenant-1'")
    log = await asyncio.to_thread(deliver, metering_db)
    await asyncio.to_thread(deliver, metering_db)
    with psycopg2.connect(metering_db) as connection:
        with pytest.raises(ValueError, match="no settlement identity"):
            support.load_handler("budget-usage-tracker").process_chat_log(
                connection,
                support._chat_log_4300(org_id="tenant-1", user_id="owner-1", account_type="service", request_id=None),
                support._bundled_rate_source(),
            )
        connection.rollback()
    expected = support._settled_cost(log)
    engine = create_async_engine(to_async_url(metering_db), poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            assert (
                await settle_usage(
                    session,
                    org_id="tenant-1",
                    request_id="legacy",
                    user_id="owner-1",
                    cost=expected,
                    total_tokens=1500,
                    entities=[("org", "tenant-1"), ("user", "owner-1"), ("agent", "owner-1")],
                    timestamp=datetime.fromisoformat(log["timestamp"].replace("Z", "+00:00")),
                )
                is False
            )
            await session.commit()
        receipts, debits, usages = ledger_rows(metering_db)
        assert receipts == [("legacy", expected)]
        assert len(debits) == 9 and all(cost == expected and count == 1 for _, _, cost, count in debits)
        assert usages == []
    finally:
        await engine.dispose()


async def test_person_alias_blocks_direct_human_across_org_but_not_service(metering_db):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "123")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "worker-2")],
        roots=[("tenant-2", "root-b")],
        anchors=["github:123"],
    )
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("UPDATE users SET cognito_sub='subject-b' WHERE id='root-b'")
        cursor.execute("UPDATE metering_scope_gates SET activation='legacy' WHERE tenant_id='tenant-2'")
        cursor.execute(
            "INSERT INTO metering_scope_aliases (alias_kind, stable_alias, scope_key, identity_revision) "
            "VALUES ('person_user', 'root-b', %s, 'github:123')",
            (person_key("github:123"),),
        )
    with pytest.raises(ValueError, match="active metering gate"):
        await asyncio.to_thread(deliver, metering_db, request_id="human-blocked", org_id="tenant-2", user_id="subject-b", account_type="human")
    await asyncio.to_thread(deliver, metering_db, request_id="service-ok", org_id="tenant-2", user_id="service-only", account_type="service")
    receipts, debits, usages = ledger_rows(metering_db)
    assert len(receipts) == 1 and receipts[0][0] == "service-ok"
    assert len(debits) == 9 and all(count == 1 for _, _, _, count in debits)
    assert usages == []

    engine = create_async_engine(to_async_url(metering_db), poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            with pytest.raises(ValueError, match="active metering gate"):
                await BudgetService(session).record_cost(
                    CostRecordRequest(
                        entity_type=EntityType.USER,
                        entity_id="subject-b",
                        model_name="anthropic.claude-3-5-sonnet-20241022-v2:0",
                        tokens_in=1,
                        tokens_out=2,
                        request_cost_usd=Decimal("0.1"),
                    ),
                    "tenant-2",
                )
            await session.rollback()
        assert ledger_rows(metering_db) == (receipts, debits, usages)
    finally:
        await engine.dispose()
