"""Historical credits coexist with durable scoped admissions."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.budget import metering_admission, pricing_correction
from src.budget.metering_policy_gates import lock_admission_policy
from src.budget.settlement import settle_usage
from src.shared.models.budget import BudgetPricingCorrection, BudgetSettlementReceipt, BudgetUsage
from src.shared.models.metering import MeteringScopeAlias, MeteringScopeGate
from src.shared.models.usage import UsageLog
from src.shared.models.vault import UserIdentity
from tests.budget import test_pricing_correction as incident
from tests.budget.test_metering_person_scopes import person_key, seed_person
from tests.budget.test_metering_scope_admission import add_gates, admission_for, context, prepare, scope_key
from tests.migrations.conftest_postgres import to_async_url
from tests.proxy.test_mantle_metering_admission import journal as base_journal

pytest_plugins = ("tests.migrations.conftest_postgres",)


@pytest.fixture
def log():
    return incident.log.__wrapped__()


@pytest.fixture
def journal():
    yield from base_journal.__wrapped__()


@pytest.fixture
async def ledger(pg_head_url, log):
    engine = create_async_engine(to_async_url(pg_head_url), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        await settle_usage(
            db,
            org_id="tenant",
            request_id="request",
            user_id="worker",
            cost="0.622440",
            total_tokens=202804,
            entities=incident.allocation_from_log(log)[1],
            timestamp=datetime(2026, 9, 28, tzinfo=UTC),
        )
        db.add(
            UsageLog(
                id="log",
                org_id="tenant",
                user_id="worker",
                account_type="service",
                team_id="team",
                department_id="dept",
                model="openai.gpt-6-sol",
                input_tokens=2,
                output_tokens=1169,
                cost_usd=Decimal("0.622440"),
                latency_ms=1,
                status_code=200,
                request_id="request",
                pricing_decision=log["pricing_decision"],
            )
        )
    yield pg_head_url, factory
    await engine.dispose()


async def correct(factory, log):
    async with factory() as db, db.begin():
        return await pricing_correction.correct_request(
            db,
            org_id="tenant",
            request_id="request",
            log=log,
            source_key="tenant/worker/receipt.json",
            actor="incident-test",
            apply=True,
        )


async def original_receipt_is_unlocked(factory):
    async with factory() as db, db.begin():
        receipt = await db.scalar(
            select(BudgetSettlementReceipt)
            .where(
                BudgetSettlementReceipt.org_id == "tenant",
                BudgetSettlementReceipt.request_id == "request",
            )
            .with_for_update(nowait=True)
        )
        assert receipt.cost_usd == Decimal("0.622440")


async def one_credit_and_original_debit(factory, log):
    async with factory() as db, db.begin():
        assert (await db.get(BudgetSettlementReceipt, ("tenant", "request"))).cost_usd == Decimal("0.622440")
        assert (await db.scalars(select(BudgetPricingCorrection))).one().credit_usd == Decimal("0.564003")
        balances = (await db.scalars(select(BudgetUsage))).all()
        assert len(balances) == 18 and all(row.total_cost_usd == Decimal("0.058437") for row in balances)
        assert not await settle_usage(
            db,
            org_id="tenant",
            request_id="request",
            user_id="worker",
            cost="0.622440",
            total_tokens=202804,
            entities=incident.allocation_from_log(log)[1],
            timestamp=datetime(2026, 9, 28, tzinfo=UTC),
        )


async def test_absent_gate_initialization_precedes_correction_receipt_lock(ledger, log):
    _, factory = ledger
    async with factory() as creator, creator.begin():
        await creator.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": "metering-gates:tenant:tenant"})
        pending = asyncio.create_task(correct(factory, log))
        await asyncio.sleep(0.05)
        assert not pending.done()
        await original_receipt_is_unlocked(factory)
        creator.add(
            MeteringScopeGate(
                scope_key=scope_key("tenant", "org", "tenant"),
                scope_kind="org",
                tenant_id="tenant",
                activation="active",
                readiness="ready",
            )
        )
    assert (await asyncio.wait_for(pending, 5))["status"] == "corrected"
    await one_credit_and_original_debit(factory, log)


async def test_correction_precedes_real_admission_without_rewriting_receipt(ledger, log, journal, monkeypatch):
    pg_url, factory = ledger
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        add_gates(cursor, "tenant", "worker")
    admission, engine = admission_for(pg_url, journal[1])
    acquired, release, trying = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_scope_lock = pricing_correction._lock_correction_scopes
    original_admission_lock = metering_admission.lock_admission_policy

    async def hold_correction(db, org_id, entry):
        await original_scope_lock(db, org_id, entry)
        acquired.set()
        await asyncio.wait_for(release.wait(), 5)

    async def observe_admission(*args, **kwargs):
        trying.set()
        return await original_admission_lock(*args, **kwargs)

    monkeypatch.setattr(pricing_correction, "_lock_correction_scopes", hold_correction)
    monkeypatch.setattr(metering_admission, "lock_admission_policy", observe_admission)
    pending = asyncio.create_task(correct(factory, log))
    try:
        await asyncio.wait_for(acquired.wait(), 5)
        admitted = asyncio.create_task(prepare(admission, context("tenant", "worker"), "fresh"))
        await asyncio.wait_for(trying.wait(), 5)
        assert not admitted.done()
        release.set()
        assert (await asyncio.wait_for(pending, 5))["status"] == "corrected"
        assert (await asyncio.wait_for(admitted, 5)).receipt.request_id == "fresh"
        await one_credit_and_original_debit(factory, log)
        assert (await correct(factory, log))["status"] == "already_corrected"
        with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state, bound_usd FROM metering_attempts WHERE request_id='fresh'")
            assert cursor.fetchall() == [("prepared", Decimal("0.006000"))]
    finally:
        release.set()
        await engine.dispose()


async def test_retained_cross_org_person_gate_precedes_correction_receipt(ledger, log):
    pg_url, factory = ledger
    await seed_person(pg_url, users=[("human", "tenant-1", "123"), ("linked", "tenant-2", "123")])
    async with factory() as db, db.begin():
        for anchor in ("github:123", "github:124"):
            db.add(MeteringScopeGate(scope_key=person_key(anchor), scope_kind="person", activation="active", readiness="ready"))
        await db.flush()
        db.add(
            MeteringScopeAlias(
                alias_kind="person_user",
                stable_alias="linked",
                scope_key=person_key("github:124"),
                identity_revision="github:124",
            )
        )
    async with factory() as other_tenant_admission, other_tenant_admission.begin():
        await lock_admission_policy(other_tenant_admission, tenant_id="other-tenant", person_anchors={"github:124"})
        pending = asyncio.create_task(correct(factory, log))
        await asyncio.sleep(0.05)
        assert not pending.done()
        await original_receipt_is_unlocked(factory)
    assert (await asyncio.wait_for(pending, 5))["status"] == "corrected"
    await one_credit_and_original_debit(factory, log)


@pytest.mark.parametrize("affected", [False, True])
async def test_missing_person_binding_refuses_only_affected_scope(ledger, log, affected):
    _, factory = ledger
    async with factory() as db, db.begin():
        db.add(MeteringScopeGate(scope_key=person_key("github:unrelated"), scope_kind="person", activation="active", readiness="ready"))
        if affected:
            db.add(
                MeteringScopeGate(
                    scope_key=scope_key("tenant", "root_user", "human"),
                    scope_kind="root_user",
                    tenant_id="tenant",
                    activation="active",
                    readiness="ready",
                )
            )
    if affected:
        with pytest.raises(ValueError, match="historical person binding cannot be verified"):
            await correct(factory, log)
        async with factory() as db:
            assert (await db.get(BudgetSettlementReceipt, ("tenant", "request"))).cost_usd == Decimal("0.622440")
            assert not (await db.scalars(select(BudgetPricingCorrection))).all()
            assert all(row.total_cost_usd == Decimal("0.622440") for row in (await db.scalars(select(BudgetUsage))).all())
    else:
        assert (await correct(factory, log))["status"] == "corrected"
        await one_credit_and_original_debit(factory, log)


async def test_replayed_receipt_races_correction_without_duplicate_or_repriced_debit(ledger, log):
    _, factory = ledger
    async with factory() as db, db.begin():
        db.add(
            MeteringScopeGate(
                scope_key=scope_key("tenant", "org", "tenant"),
                scope_kind="org",
                tenant_id="tenant",
                activation="active",
                readiness="ready",
            )
        )

    async def replay():
        async with factory() as db, db.begin():
            return await settle_usage(
                db,
                org_id="tenant",
                request_id="request",
                user_id="worker",
                cost="0.622440",
                total_tokens=202804,
                entities=incident.allocation_from_log(log)[1],
                timestamp=datetime(2026, 9, 28, tzinfo=UTC),
            )

    async with factory() as held, held.begin():
        await held.scalar(
            select(BudgetSettlementReceipt)
            .where(
                BudgetSettlementReceipt.org_id == "tenant",
                BudgetSettlementReceipt.request_id == "request",
            )
            .with_for_update()
        )
        correction = asyncio.create_task(correct(factory, log))
        duplicate = asyncio.create_task(replay())
        await asyncio.sleep(0.05)
        assert not correction.done()
    corrected, claimed = await asyncio.wait_for(asyncio.gather(correction, duplicate), 5)
    assert corrected["status"] == "corrected" and claimed is False
    await one_credit_and_original_debit(factory, log)
    async with factory() as db:
        audit = (await db.scalars(select(BudgetPricingCorrection))).one()
        assert audit.original_decision == log["pricing_decision"]
        assert (await db.get(UsageLog, "log")).cost_usd == Decimal("0.058437")


async def test_identity_change_during_correction_refuses_stale_person_allocation(ledger, log):
    pg_url, factory = ledger
    await seed_person(pg_url, users=[("human", "tenant-1", "123"), ("linked", "tenant-2", "123")])
    async with factory() as db, db.begin():
        db.add(MeteringScopeGate(scope_key=person_key("github:123"), scope_kind="person", activation="active", readiness="ready"))
    async with factory() as writer:
        await writer.execute(update(UserIdentity).where(UserIdentity.user_id == "linked").values(provider_user_id="124"))
        pending = asyncio.create_task(correct(factory, log))
        await asyncio.sleep(0.05)
        assert not pending.done()
        await original_receipt_is_unlocked(factory)
        await writer.commit()
    with pytest.raises(ValueError, match="person binding changed during correction"):
        await asyncio.wait_for(pending, 3)
    async with factory() as db:
        assert (await db.get(BudgetSettlementReceipt, ("tenant", "request"))).cost_usd == Decimal("0.622440")
        assert not (await db.scalars(select(BudgetPricingCorrection))).all()
    assert (await correct(factory, log))["status"] == "corrected"
    await one_credit_and_original_debit(factory, log)
