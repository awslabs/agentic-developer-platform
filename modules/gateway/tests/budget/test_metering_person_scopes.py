"""Person aliases and strict policy barriers in durable admission."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import psycopg2
import pytest
import rfc8785
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.budget.metering_admission import MeteringAccountingUnresolvedError, MeteringAdmissionError, MeteringBudgetExceededError
from src.budget.reservations import ReservationTarget
from src.budget.run_binding import RunBinding
from src.orchestration.provider_quotes import request_digest
from src.shared.models.budget import PersonBudgetConfig
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity
from tests.budget import test_metering_scope_admission as scope_fixtures
from tests.budget.test_metering_scope_admission import add_gates, admission_for, context, prepare, scope_key
from tests.migrations.conftest_postgres import to_async_url

pytest_plugins = ("tests.budget.test_metering_scope_admission",)


@pytest.fixture
def metering_db(pg_head_url):
    return scope_fixtures.metering_db.__wrapped__(pg_head_url)


@pytest.fixture
def journal():
    yield from scope_fixtures.journal.__wrapped__()


def person_key(anchor):
    return f"v1:person:{request_digest(rfc8785.dumps(['person', anchor]))}"


def rooted_context(tenant_id, worker, human, *, flow_id=None, enforcement_enabled=True):
    caller = context(tenant_id, worker)
    caller.attributed_user_id = human
    caller._protected_run_binding = RunBinding(
        run_id="run-1",
        correlation_id="chain-1",
        tenant_id=tenant_id,
        user_id=worker,
        root_human_id=human,
        is_human_rooted=True,
        flow_id=flow_id,
    )
    caller._budget_enforcement_enabled = enforcement_enabled
    return caller


async def seed_person(pg_url, *, users, cap=None):
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all([Organization(id=tenant, name=tenant) for tenant in ("tenant-1", "tenant-2")])
            await session.flush()
            for user_id, tenant_id, github_id in users:
                session.add(User(id=user_id, org_id=tenant_id, team_id="", email=f"{user_id}@example.test"))
                session.add(
                    UserIdentity(
                        user_id=user_id, org_id=tenant_id, team_id="", provider="github", provider_user_id=github_id, verification_method="oauth"
                    )
                )
            if cap is not None:
                session.add(
                    PersonBudgetConfig(
                        person_anchor="github:123",
                        period_type="monthly",
                        budget_amount_usd=Decimal(cap),
                        enforcement_mode="hard",
                        authored_by_user_id="root-a",
                    )
                )
            await session.commit()
    finally:
        await engine.dispose()


def activate_person_scopes(pg_url, *, workers, roots, anchors):
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        for tenant, worker in workers:
            if tenant != "tenant-1":
                add_gates(cursor, tenant, worker)
        for tenant, human in roots:
            cursor.execute(
                "INSERT INTO metering_scope_gates (scope_key, scope_kind, tenant_id, activation, readiness) "
                "VALUES (%s, 'root_user', %s, 'active', 'ready') ON CONFLICT DO NOTHING",
                (scope_key(tenant, "root_user", human), tenant),
            )
        for anchor in anchors:
            cursor.execute(
                "INSERT INTO metering_scope_gates (scope_key, scope_kind, tenant_id, activation, readiness) "
                "VALUES (%s, 'person', NULL, 'active', 'ready') ON CONFLICT DO NOTHING",
                (person_key(anchor),),
            )


async def test_relinked_human_cannot_escape_cross_org_unknown_hold(metering_db, journal):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "125")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "owner-2")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-a"), ("tenant-2", "root-b")],
        anchors=["github:123", "github:124", "github:125"],
    )
    admission, engine = admission_for(metering_db, journal)
    try:
        await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "root-before-link")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='root-before-link'")
            cursor.execute("UPDATE user_identities SET provider_user_id='124' WHERE user_id='root-a'")
        with pytest.raises(MeteringAccountingUnresolvedError):
            await prepare(admission, rooted_context("tenant-2", "owner-2", "root-a", enforcement_enabled=False), "denied-after-link")
        await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "unrelated-person")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT tenant_id, request_id FROM metering_attempts ORDER BY request_id")
            assert cursor.fetchall() == [("tenant-1", "root-before-link"), ("tenant-2", "unrelated-person")]
            cursor.execute("SELECT scope_key FROM metering_scope_aliases WHERE stable_alias='root-a'")
            assert {row[0] for row in cursor.fetchall()} == {person_key("github:123"), person_key("github:124")}
    finally:
        await engine.dispose()


async def test_person_cap_counts_open_bounds_across_organizations(metering_db, journal):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "123")], cap="0.01")
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "owner-2")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-b")],
        anchors=["github:123"],
    )
    admission, engine = admission_for(metering_db, journal)
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO budget_usage (id, org_id, entity_type, entity_id, period_start, period_type, "
            "total_cost_usd, total_tokens, request_count) VALUES (%s, 'tenant-1', 'root_user', 'root-a', "
            "%s, 'monthly', %s, 0, 1)",
            (str(uuid4()), datetime.now(UTC).date().replace(day=1), Decimal("0.002")),
        )
    try:
        await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "first-person")
        with pytest.raises(MeteringBudgetExceededError):
            await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "over-person-cap")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT tenant_id, request_id FROM metering_attempts")
            assert cursor.fetchall() == [("tenant-1", "first-person")]
            cursor.execute("SELECT count(*) FROM metering_attempt_scopes WHERE scope_key=%s", (person_key("github:123"),))
            assert cursor.fetchone() == (1,)
        await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b", enforcement_enabled=False), "off-mode-cap")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT tenant_id, request_id FROM metering_attempts ORDER BY request_id")
            assert cursor.fetchall() == [("tenant-1", "first-person"), ("tenant-2", "off-mode-cap")]
            cursor.execute("SELECT total_cost_usd, request_count FROM budget_usage WHERE entity_type='root_user'")
            assert cursor.fetchall() == [(Decimal("0.002000"), 1)]
            cursor.execute("SELECT count(*) FROM budget_settlement_receipts")
            assert cursor.fetchone() == (0,)
        with pytest.raises(MeteringBudgetExceededError):
            await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "cap-still-enforced")
    finally:
        await engine.dispose()


async def test_strict_policy_and_unresolved_person_refuse_before_sql_even_if_caps_off(metering_db, journal):
    admission, engine = admission_for(metering_db, journal)
    try:
        with pytest.raises(MeteringAdmissionError, match="strict policy"):
            await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a", flow_id="flow-1", enforcement_enabled=False), "denied-policy")
        policy = context()
        policy._budget_enforcement_enabled = False
        policy._policy_flow_target = ReservationTarget(
            org_id="tenant-1",
            entity_type="flow",
            entity_id="flow-1",
            period_type="monthly",
            period_start=datetime.now(UTC).date().replace(day=1).isoformat(),
            headroom_usd=Decimal("1"),
        )
        with pytest.raises(MeteringAdmissionError, match="strict policy"):
            await prepare(admission, policy, "denied-policy-target")
        mismatch = rooted_context("tenant-1", "owner-1", "root-a", enforcement_enabled=False)
        mismatch._protected_run_binding = replace(mismatch._protected_run_binding, tenant_id="tenant-2")
        with pytest.raises(MeteringAdmissionError, match="run tenant"):
            await prepare(admission, mismatch, "denied-wrong-tenant")
        with pytest.raises(MeteringAdmissionError, match="identity is unresolved"):
            await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "denied-missing-person")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM metering_attempts")
            assert cursor.fetchone() == (0,)
    finally:
        await engine.dispose()


async def test_fusion_denial_retains_both_historical_anchors_after_split(metering_db, journal):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "124"), ("root-c", "tenant-2", "127")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "owner-2")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-b"), ("tenant-2", "root-c")],
        anchors=["github:123", "github:124", "github:125", "github:126", "github:127"],
    )
    admission, engine = admission_for(metering_db, journal)
    try:
        await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "held-a")
        await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "held-b")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='held-a'")
            cursor.execute("UPDATE user_identities SET provider_user_id='125' WHERE user_id IN ('root-a','root-b')")
        with pytest.raises(MeteringAccountingUnresolvedError):
            await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b", enforcement_enabled=False), "denied-fused")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT scope_key FROM metering_scope_aliases WHERE stable_alias='root-b'")
            assert {row[0] for row in cursor.fetchall()} == {person_key("github:123"), person_key("github:124"), person_key("github:125")}
            cursor.execute("UPDATE user_identities SET provider_user_id='126' WHERE user_id='root-b'")
        with pytest.raises(MeteringAccountingUnresolvedError):
            await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b", enforcement_enabled=False), "denied-after-split")
        await prepare(admission, rooted_context("tenant-2", "owner-2", "root-c", enforcement_enabled=False), "independent-root")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT request_id FROM metering_attempts ORDER BY request_id")
            assert [row[0] for row in cursor.fetchall()] == ["held-a", "held-b", "independent-root"]
            cursor.execute("SELECT count(*) FROM budget_settlement_receipts")
            assert cursor.fetchone() == (0,)
    finally:
        await engine.dispose()


async def test_hierarchy_move_keeps_original_team_and_department_holds_when_caps_off(metering_db, journal):
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        add_gates(cursor, "tenant-2", "owner-1")
        for kind in ("team", "department"):
            for identity in ("old", "new"):
                cursor.execute(
                    "INSERT INTO metering_scope_gates (scope_key, scope_kind, tenant_id, activation, readiness) "
                    "VALUES (%s, %s, 'tenant-1', 'active', 'ready')",
                    (scope_key("tenant-1", kind, identity), kind),
                )
    admission, engine = admission_for(metering_db, journal)
    try:
        before = context()
        before._budget_enforcement_enabled = False
        before.team_id = before.department_id = "old"
        await prepare(admission, before, "original-hierarchy")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='original-hierarchy'")
        after = context()
        after._budget_enforcement_enabled = False
        after.team_id = after.department_id = "new"
        with pytest.raises(MeteringAccountingUnresolvedError):
            await prepare(admission, after, "denied-after-move")
        await prepare(admission, context("tenant-2", "owner-1"), "unrelated-tenant")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT scope_key FROM metering_attempt_scopes WHERE request_id='original-hierarchy'")
            scopes = {row[0] for row in cursor.fetchall()}
            assert {scope_key("tenant-1", kind, "old") for kind in ("team", "department")} <= scopes
            assert not {scope_key("tenant-1", kind, "new") for kind in ("team", "department")} & scopes
            cursor.execute("SELECT request_id FROM metering_attempts ORDER BY request_id")
            assert [row[0] for row in cursor.fetchall()] == ["original-hierarchy", "unrelated-tenant"]
    finally:
        await engine.dispose()


async def test_rooted_receipt_settles_once_without_person_duplicate_debit(metering_db, journal):
    from tests.budget.test_metering_settlement_receipts import complete

    await seed_person(metering_db, users=[("root-a", "tenant-1", "123")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1")],
        roots=[("tenant-1", "root-a")],
        anchors=["github:123"],
    )
    admission, engine = admission_for(metering_db, journal)
    try:
        attempt, receipt, decision = await complete(admission, rooted_context("tenant-1", "owner-1", "root-a"), "rooted-final")
        assert await admission.settle_receipt(attempt, receipt) is True
        assert await admission.settle_receipt(attempt, receipt) is False
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT entity_type, total_cost_usd, request_count FROM budget_usage WHERE period_type='daily'")
            debits = cursor.fetchall()
            assert {kind for kind, _, _ in debits} == {"org", "user", "agent", "root_user"}
            assert all(cost == decision.ledger_cost and count == 1 for _, cost, count in debits)
            cursor.execute("SELECT count(*) FROM budget_settlement_receipts WHERE request_id='rooted-final'")
            assert cursor.fetchone() == (1,)
            cursor.execute("SELECT count(*) FROM usage_logs WHERE request_id='rooted-final'")
            assert cursor.fetchone() == (1,)
    finally:
        await engine.dispose()
