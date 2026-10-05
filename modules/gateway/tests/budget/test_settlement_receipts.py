# ruff: noqa: F811
"""Real PostgreSQL replay, race, rollback and tenant boundaries for A13/S18."""

import asyncio
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.budget.settlement import settle_usage
from src.shared.models.budget import BudgetSettlementReceipt, BudgetUsage
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401, F811


@pytest.fixture
async def ledger(pg_url):
    engine = create_async_engine(to_async_url(pg_url))
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: BudgetSettlementReceipt.__table__.create(sync))
        await conn.run_sync(lambda sync: BudgetUsage.__table__.create(sync))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def debit(ledger, request="request", org="tenant", user="owner", cost="0.123456", fail=False):
    async with ledger() as db:
        async with db.begin():
            fresh = await settle_usage(
                db, org_id=org, request_id=request, user_id=user, cost=Decimal(cost), total_tokens=12, entities=[("user", user), ("org", org)]
            )
            if fail:
                raise RuntimeError("interrupted fanout")
            return fresh


@pytest.mark.asyncio
async def test_concurrent_replay_is_one_atomic_debit(ledger):
    results = await asyncio.gather(*(debit(ledger) for _ in range(8)))
    assert results.count(True) == 1
    async with ledger() as db:
        rows = (await db.execute(select(BudgetUsage))).scalars().all()
        assert len(rows) == 6
        assert all(row.request_count == 1 and row.total_tokens == 12 and row.total_cost_usd == Decimal("0.123456") for row in rows)


@pytest.mark.asyncio
async def test_rollback_allows_redelivery_and_tenants_do_not_alias(ledger):
    with pytest.raises(RuntimeError):
        await debit(ledger, fail=True)
    assert await debit(ledger)
    assert await debit(ledger, org="other")
    assert await debit(ledger, request="another")
    async with ledger() as db:
        rows = (await db.execute(select(BudgetUsage))).scalars().all()
        assert len(rows) == 12
        assert all(row.request_count == (2 if row.org_id == "tenant" else 1) for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [{"cost": "0.5"}, {"user": "intruder"}])
async def test_conflicting_replay_cannot_change_debit(ledger, changed):
    assert await debit(ledger)
    with pytest.raises(ValueError, match="conflicting"):
        await debit(ledger, **changed)
    assert not await debit(ledger)


@pytest.mark.asyncio
async def test_tracker_replay_and_gateway_share_receipt(ledger, pg_url):
    import importlib

    import psycopg2
    from sqlalchemy import text

    support = importlib.import_module("tests.lambda.test_budget_usage_tracker")
    handler = support.load_handler("budget-usage-tracker")
    log = support._chat_log_4300()
    amount = support._settled_cost(log)
    async with ledger() as db:
        await db.execute(text("CREATE TABLE usage_logs (request_id text, org_id text, user_id text, cost_usd numeric, chat_log_s3_key text)"))
        await db.execute(text("INSERT INTO usage_logs VALUES ('req-4300', 'other-tenant', 'other-owner', 0, NULL)"))
        await db.commit()

    def deliver():
        with psycopg2.connect(pg_url) as conn:
            handler.process_chat_log(conn, log, support._bundled_rate_source(), chat_log_s3_key="transcript.json")

    await asyncio.to_thread(deliver)
    await asyncio.to_thread(deliver)
    async with ledger() as db:
        assert not await settle_usage(
            db,
            org_id=log["org_id"],
            request_id=log["request_id"],
            user_id=log["user_id"],
            cost=amount,
            total_tokens=1500,
            entities=[("user", log["user_id"]), ("org", log["org_id"])],
            timestamp=datetime.fromisoformat(log["timestamp"]),
        )
        await db.commit()
        rows = (await db.execute(select(BudgetUsage))).scalars().all()
        assert len(rows) == 6
        assert all(row.request_count == 1 and row.total_tokens == 1500 and row.total_cost_usd == amount for row in rows)
        unrelated = (await db.execute(text("SELECT cost_usd, chat_log_s3_key FROM usage_logs"))).one()
        assert unrelated == (0, None)


@pytest.mark.asyncio
async def test_tracker_fanout_failure_rolls_back_receipt(ledger, pg_url, monkeypatch):
    import importlib

    import psycopg2
    from sqlalchemy import text

    support = importlib.import_module("tests.lambda.test_budget_usage_tracker")
    handler = support.load_handler("budget-usage-tracker")
    log = support._chat_log_4300()
    async with ledger() as db:
        await db.execute(text("CREATE TABLE usage_logs (request_id text, org_id text, user_id text, cost_usd numeric, chat_log_s3_key text)"))
        await db.commit()
    record = handler.upsert_budget_usage
    calls = 0

    def fail_midway(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("fanout interrupted")
        return record(*args, **kwargs)

    monkeypatch.setattr(handler, "upsert_budget_usage", fail_midway)

    def deliver():
        with psycopg2.connect(pg_url) as conn:
            handler.process_chat_log(conn, log, support._bundled_rate_source())

    with pytest.raises(RuntimeError, match="fanout"):
        await asyncio.to_thread(deliver)
    async with ledger() as db:
        assert (await db.execute(select(BudgetSettlementReceipt))).scalars().all() == []
        assert (await db.execute(select(BudgetUsage))).scalars().all() == []
    monkeypatch.setattr(handler, "upsert_budget_usage", record)
    await asyncio.to_thread(deliver)


@pytest.mark.asyncio
async def test_tracker_receipt_does_not_drop_first_gateway_usage_row(ledger):
    from datetime import UTC, datetime

    from pricing_policy import RoutingEvidence, build_pricing_decision, load_snapshot, normalize_usage
    from src.shared.models.usage import UsageLog
    from src.shared.schemas.auth import TokenContext
    from src.usage.service import UsageService

    async with ledger() as db:
        await db.run_sync(lambda sync: UsageLog.__table__.create(sync.connection()))
        await db.commit()
    context = TokenContext(org_id="tenant", user_id="owner", team_id="team", department_id="dept", account_type="human", expires_at=datetime.now(UTC))
    snapshot = load_snapshot()
    decision = build_pricing_decision(
        request_id="request",
        org_id="tenant",
        usage=normalize_usage({"input_tokens": 10, "output_tokens": 2}, api_format="openai"),
        evidence=RoutingEvidence(
            original_model_id="openai.gpt-5.6-sol",
            billing_model_id="openai.gpt-5.6-sol",
            served_service_tier_raw="standard",
            geography="in_region",
            endpoint_region="us-east-1",
        ),
        rows=snapshot.rates,
        snapshot=snapshot,
    )
    from src.budget.settlement import settle_priced_usage

    async with ledger() as db:
        assert await settle_priced_usage(db, context=context, request_id="request", decision=decision)
        await db.commit()

    async def log():
        async with ledger() as db:
            await UsageService(db).log_request(
                context=context,
                model="measured",
                input_tokens=10,
                output_tokens=2,
                cost_usd=decision.ledger_cost,
                latency_ms=1,
                status_code=200,
                request_id="request",
                pricing_decision=decision,
            )

    await asyncio.gather(*(log() for _ in range(4)))
    async with ledger() as db:
        rows = (await db.execute(select(UsageLog))).scalars().all()
        assert len(rows) == 1
        assert rows[0].cost_usd == decision.ledger_cost


@pytest.mark.asyncio
async def test_historical_transcript_is_not_redebited(ledger, monkeypatch):
    import importlib
    from unittest.mock import MagicMock

    support = importlib.import_module("tests.lambda.test_budget_usage_tracker")
    handler = support.load_handler("budget-usage-tracker")
    legacy = support._chat_log_4300()
    legacy.pop("settlement_version")
    conn = MagicMock()
    with pytest.raises(ValueError, match="Legacy transcript"):
        handler.process_chat_log(conn, legacy, support._bundled_rate_source())
    conn.cursor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["period", "entities"])
async def test_replay_cannot_reallocate_period_or_hierarchy(ledger, change):
    from datetime import UTC, datetime, timedelta

    timestamp = datetime(2026, 9, 24, 23, 59, tzinfo=UTC)
    params = dict(
        org_id="tenant",
        user_id="owner",
        request_id="midnight",
        cost=Decimal("0.01"),
        total_tokens=10,
        timestamp=timestamp,
        entities=[("user", "owner"), ("team", "team")],
    )
    async with ledger() as db:
        assert await settle_usage(db, **params)
        await db.commit()
    params.update(
        {"timestamp": timestamp + timedelta(minutes=2)} if change == "period" else {"entities": [("user", "owner"), ("team", "other-team")]}
    )
    async with ledger() as db:
        with pytest.raises(ValueError, match="conflicting"):
            await settle_usage(db, **params)
        await db.rollback()
        rows = (await db.execute(select(BudgetUsage))).scalars().all()
        assert len(rows) == 6
        assert all(row.request_count == 1 for row in rows)


@pytest.mark.asyncio
async def test_migration_creates_receipt_contract_and_preserves_on_rollback(pg_url, monkeypatch):
    import importlib.util
    from pathlib import Path

    from sqlalchemy import inspect

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    spec = importlib.util.spec_from_file_location(
        "receipt_migration", Path(__file__).resolve().parents[2] / "alembic/versions/074_budget_settlement_receipts.py"
    )
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_async_engine(to_async_url(pg_url))
    async with engine.begin() as connection:

        def upgrade(sync):
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
            migration.upgrade()
            columns = {column["name"] for column in inspect(sync).get_columns("budget_settlement_receipts")}
            assert columns == {"org_id", "request_id", "user_id", "cost_usd", "total_tokens", "allocation_key"}
            with pytest.raises(RuntimeError, match="survive rollback"):
                migration.downgrade()
            assert inspect(sync).has_table("budget_settlement_receipts")

        await connection.run_sync(upgrade)
    await engine.dispose()


@pytest.mark.asyncio
async def test_receipt_scope_evidence_is_atomic_and_replay_does_not_double_debit(ledger):
    params = dict(org_id="tenant", request_id="receipt", user_id="owner", cost=Decimal("1"), total_tokens=10, entities=[("user", "owner")])
    async with ledger() as db:
        assert await settle_usage(db, **params, reservation_scope_keys=["flow-models"])
        await db.rollback()
        assert await db.get(BudgetSettlementReceipt, ("tenant", "receipt")) is None
        assert (await db.scalars(select(BudgetUsage))).all() == []
        assert await settle_usage(db, **params)  # A legacy/tracker receipt has no scope authority.
        await db.commit()
        row = await db.get(BudgetSettlementReceipt, ("tenant", "receipt"))
        assert row.reservation_scope_keys is None
        assert not await settle_usage(db, **params, reservation_scope_keys=["flow-models"])
        await db.commit()
        assert not await settle_usage(db, **params, reservation_scope_keys=["flow-models", "parent-models"])
        await db.commit()
        await db.refresh(row)
        assert row.reservation_scope_keys == ["flow-models", "parent-models"]
        assert all(row.request_count == 1 and row.total_cost_usd == 1 for row in await db.scalars(select(BudgetUsage)))
        with pytest.raises(ValueError, match="conflicting"):
            await settle_usage(db, **{**params, "cost": Decimal("2")}, reservation_scope_keys=["unrelated"])
        await db.rollback()
        await db.refresh(row)
        assert row.reservation_scope_keys == ["flow-models", "parent-models"]
