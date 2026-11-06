"""Budget and person-cap writers cannot race the active admission denominator."""

import asyncio
from decimal import Decimal

import psycopg2
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.admin.schemas import BudgetPeriodSetRequest
from src.admin.service import AdminService
from src.budget.metering_admission import MeteringBudgetExceededError
from src.budget.metering_policy_gates import lock_policy_mutation
from src.budget.person_cap_routes import _upsert_cap, _upsert_default
from src.budget.service import BudgetService
from src.shared.schemas.budget import BudgetUpdateRequest
from tests.budget import test_metering_scope_admission as scope_fixtures
from tests.budget.test_metering_person_scopes import activate_person_scopes, rooted_context, seed_person
from tests.budget.test_metering_scope_admission import BODY, add_gates, admission_for, context, insert_budget, prepare
from tests.proxy.test_mantle_metering_admission import MODEL

pytest_plugins = ("tests.budget.test_metering_scope_admission",)


@pytest.fixture
def metering_db(pg_head_url):
    return scope_fixtures.metering_db.__wrapped__(pg_head_url)


@pytest.fixture
def journal():
    yield from scope_fixtures.journal.__wrapped__()


async def test_budget_reduction_waits_for_admission_and_preserves_original_hold(metering_db, journal, monkeypatch):
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.02"))
        cursor.execute("SELECT id FROM budget_configs WHERE org_id='tenant-1'")
        budget_id = cursor.fetchone()[0]
    admission, engine = admission_for(metering_db, journal)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    entered, release = asyncio.Event(), asyncio.Event()
    original_check = admission._check_scope

    async def paused_check(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 3)
        return await original_check(*args, **kwargs)

    monkeypatch.setattr(admission, "_check_scope", paused_check)

    async def reduce():
        async with factory() as writer:
            return await BudgetService(writer).update_budget(budget_id, BudgetUpdateRequest(budget_amount_usd=Decimal("0.01")), "tenant-1")

    try:
        admitted = asyncio.create_task(prepare(admission, context(), "budget-before-reduction"))
        await asyncio.wait_for(entered.wait(), 3)
        mutation = asyncio.create_task(reduce())
        with pytest.raises(DBAPIError) as blocked:
            await asyncio.wait_for(mutation, 3)
        assert blocked.value.orig.sqlstate == "55P03"
        assert "lock timeout" in str(blocked.value.orig)
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT budget_amount_usd FROM budget_configs WHERE id=%s", (budget_id,))
            assert cursor.fetchone() == (Decimal("0.02"),)
        release.set()
        await asyncio.wait_for(admitted, 3)
        assert (await asyncio.wait_for(reduce(), 3)).budget_amount_usd == Decimal("0.01")
        with pytest.raises(MeteringBudgetExceededError):
            await prepare(admission, context(), "budget-after-reduction")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT budget_amount_usd FROM budget_configs WHERE id=%s", (budget_id,))
            assert cursor.fetchone() == (Decimal("0.01"),)
            cursor.execute("SELECT request_id, state, bound_usd FROM metering_attempts ORDER BY request_id")
            assert cursor.fetchall() == [("budget-before-reduction", "prepared", Decimal("0.006000"))]
            cursor.execute("SELECT count(*) FROM metering_attempt_scopes WHERE request_id='budget-before-reduction'")
            assert cursor.fetchone() == (3,)
    finally:
        release.set()
        await engine.dispose()


async def test_person_cap_reduction_fails_closed_during_admission_across_tenants(metering_db, journal, monkeypatch):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "123")], cap="0.02")
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "owner-2")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-b")],
        anchors=["github:123"],
    )
    admission, engine = admission_for(metering_db, journal)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    caller = rooted_context("tenant-1", "owner-1", "root-a")
    entered, release = asyncio.Event(), asyncio.Event()
    original_check = admission._check_scope

    async def paused_check(*args, **kwargs):
        if not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 3)
        return await original_check(*args, **kwargs)

    monkeypatch.setattr(admission, "_check_scope", paused_check)

    async def reduce():
        async with factory() as writer:
            await _upsert_cap(
                writer,
                person_anchor="github:123",
                period_type="monthly",
                amount=Decimal("0.01"),
                authored_by_user_id="root-a",
            )

    try:
        admitted = asyncio.create_task(prepare(admission, caller, "person-before-reduction"))
        await asyncio.wait_for(entered.wait(), 3)
        mutation = asyncio.create_task(reduce())
        # The production lock wait is bounded to 500 ms. Hold admission until the
        # writer refuses, instead of assuming all remaining admission queries
        # finish inside that window on a shared CI runner. The original cap and
        # the admitted reservation must survive; a fresh write can then succeed.
        with pytest.raises(DBAPIError) as blocked:
            await asyncio.wait_for(mutation, 3)
        assert blocked.value.orig.sqlstate == "55P03"
        assert "lock timeout" in str(blocked.value.orig)
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT budget_amount_usd FROM person_budget_configs WHERE person_anchor='github:123'")
            assert cursor.fetchone() == (Decimal("0.02"),)
        release.set()
        await asyncio.wait_for(admitted, 3)
        await asyncio.wait_for(reduce(), 3)
        with pytest.raises(MeteringBudgetExceededError):
            await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "person-after-reduction")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT budget_amount_usd FROM person_budget_configs WHERE person_anchor='github:123'")
            assert cursor.fetchone() == (Decimal("0.01"),)
            cursor.execute("SELECT request_id, state FROM metering_attempts ORDER BY request_id")
            assert cursor.fetchall() == [("person-before-reduction", "prepared")]
            cursor.execute("SELECT scope_key, bound_usd FROM metering_attempt_scopes WHERE request_id='person-before-reduction'")
            scopes = cursor.fetchall()
            assert len(scopes) == 5 and all(bound == Decimal("0.006000") for _, bound in scopes)
    finally:
        release.set()
        await engine.dispose()


async def test_held_tenant_person_and_default_writers_do_not_block_disjoint_admissions(metering_db, journal):
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        add_gates(cursor, "tenant-2", "owner-2")
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "456")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-b")],
        anchors=["github:123", "github:456"],
    )
    admission, engine = admission_for(metering_db, journal)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as writer:
            await lock_policy_mutation(writer, tenant_id="tenant-1", scope_kind="org", identity="tenant-1")
            intersecting = asyncio.create_task(prepare(admission, context(), "tenant-blocked"))
            await asyncio.wait_for(prepare(admission, context("tenant-2", "owner-2"), "tenant-independent"), 3)
            # The overlapping request must refuse at the production lock timeout,
            # even if the independent admission takes longer than that timeout.
            with pytest.raises(DBAPIError) as blocked:
                await asyncio.wait_for(intersecting, 3)
            assert blocked.value.orig.sqlstate == "55P03"
            assert "lock timeout" in str(blocked.value.orig)
            await writer.commit()
            await asyncio.wait_for(prepare(admission, context(), "tenant-blocked"), 3)
        async with factory() as writer:
            await lock_policy_mutation(writer, person_anchor="github:123")
            intersecting = asyncio.create_task(prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "person-blocked"))
            await asyncio.wait_for(prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "person-independent"), 3)
            # The overlapping request must refuse at the production lock timeout,
            # even if the independent admission takes longer than that timeout.
            with pytest.raises(DBAPIError) as blocked:
                await asyncio.wait_for(intersecting, 3)
            assert blocked.value.orig.sqlstate == "55P03"
            assert "lock timeout" in str(blocked.value.orig)
            await writer.commit()
            await asyncio.wait_for(prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "person-blocked"), 3)
        async with factory() as writer:
            await lock_policy_mutation(writer, default_scope="org:tenant-1")
            intersecting = asyncio.create_task(prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "default-blocked"))
            await asyncio.wait_for(prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "default-independent"), 3)
            # The overlapping request must refuse at the production lock timeout,
            # even if the independent admission takes longer than that timeout.
            with pytest.raises(DBAPIError) as blocked:
                await asyncio.wait_for(intersecting, 3)
            assert blocked.value.orig.sqlstate == "55P03"
            assert "lock timeout" in str(blocked.value.orig)
            await writer.commit()
            await asyncio.wait_for(prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "default-blocked"), 3)
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT request_id, tenant_id, state FROM metering_attempts ORDER BY request_id")
            assert cursor.fetchall() == [
                ("default-blocked", "tenant-1", "prepared"),
                ("default-independent", "tenant-2", "prepared"),
                ("person-blocked", "tenant-1", "prepared"),
                ("person-independent", "tenant-2", "prepared"),
                ("tenant-blocked", "tenant-1", "prepared"),
                ("tenant-independent", "tenant-2", "prepared"),
            ]
    finally:
        await engine.dispose()


@pytest.mark.parametrize("limit_kind", ["person", "default"])
async def test_cap_change_after_prepare_refuses_arm_without_losing_hold(metering_db, journal, limit_kind):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123")], cap="0.02" if limit_kind == "person" else None)
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1")],
        roots=[("tenant-1", "root-a")],
        anchors=["github:123"],
    )
    admission, engine = admission_for(metering_db, journal)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    caller = rooted_context("tenant-1", "owner-1", "root-a")
    try:
        first = await prepare(admission, caller, "first-prepared")
        second = await prepare(admission, caller, "second-prepared")
        async with factory() as writer:
            if limit_kind == "person":
                await _upsert_cap(
                    writer,
                    person_anchor="github:123",
                    period_type="monthly",
                    amount=Decimal("0.01"),
                    authored_by_user_id="root-a",
                )
            else:
                await _upsert_default(
                    writer,
                    scope_type="org",
                    scope_id_org="tenant-1",
                    scope_id_team=None,
                    period_type="monthly",
                    amount=Decimal("0.01"),
                    authored_by_user_id="root-a",
                )
        with pytest.raises(MeteringBudgetExceededError):
            await admission.arm(second, submitted=BODY, model=MODEL, destination="https://provider.invalid/responses")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT request_id, state FROM metering_attempts ORDER BY request_id")
            assert cursor.fetchall() == [("first-prepared", "prepared"), ("second-prepared", "prepared")]
            cursor.execute("SELECT count(*), sum(bound_usd) FROM metering_attempt_scopes WHERE scope_key LIKE 'v1:person:%'")
            assert cursor.fetchone() == (2, Decimal("0.012000"))
            cursor.execute("SELECT count(*) FROM budget_settlement_receipts")
            assert cursor.fetchone() == (0,)
        await admission.cancel_unarmed(second)
        permit = await admission.arm(first, submitted=BODY, model=MODEL, destination="https://provider.invalid/responses")
        assert permit.request_id == first.receipt.request_id
    finally:
        await engine.dispose()


async def test_admin_exact_budget_insert_after_prepare_rechecks_arm(metering_db, journal):
    admission, engine = admission_for(metering_db, journal)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        first = await prepare(admission, context(), "admin-first")
        second = await prepare(admission, context(), "admin-second")
        async with factory() as writer:
            await AdminService(writer).set_exact_budget(
                "tenant-1",
                "org",
                "tenant-1",
                "monthly",
                BudgetPeriodSetRequest(budget_amount_usd="0.01", enforcement_mode="hard", expect_absent=True),
            )
        with pytest.raises(MeteringBudgetExceededError):
            await admission.arm(second, submitted=BODY, model=MODEL, destination="https://provider.invalid/responses")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT budget_amount_usd FROM budget_configs WHERE org_id='tenant-1' AND period_type='monthly'")
            assert cursor.fetchone() == (Decimal("0.01"),)
            cursor.execute("SELECT request_id, state FROM metering_attempts ORDER BY request_id")
            assert cursor.fetchall() == [("admin-first", "prepared"), ("admin-second", "prepared")]
            cursor.execute("SELECT count(*) FROM budget_settlement_receipts")
            assert cursor.fetchone() == (0,)
        await admission.cancel_unarmed(second)
        assert (await admission.arm(first, submitted=BODY, model=MODEL, destination="https://provider.invalid/responses")).request_id == "admin-first"
    finally:
        await engine.dispose()
