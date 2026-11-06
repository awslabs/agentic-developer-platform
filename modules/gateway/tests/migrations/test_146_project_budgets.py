"""Real PostgreSQL migration, Project capture races and settled shared budgets."""

import asyncio
from dataclasses import replace
from decimal import Decimal

import fakeredis.aioredis
import psycopg2
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from src.budget.project_budget import prepare_project_budget, reconcile_project_budget
from src.budget.reservations import ReservationStore
from src.budget.settlement import settle_usage
from src.orchestration.models import OrchestrationFlow
from src.products.models import ProjectRun
from src.products.work_scope import ProjectAttribution, ProjectScopeError, bind_run
from src.shared.models.organization import Organization, User
from tests.migrations.conftest_postgres import downgrade, to_async_url, upgrade

pytest_plugins = ("tests.migrations.conftest_postgres",)


def seed(url):
    engine = create_engine(url)
    with Session(engine) as db:
        db.add(Organization(id="tenant", name="Fixture"))
        db.flush()
        db.add(User(id="owner", org_id="tenant", team_id="team", email="owner@example.test", user_kind="human"))
        db.commit()
    engine.dispose()
    with psycopg2.connect(url) as conn, conn.cursor() as cur:
        for identity in ("existing", "empty"):
            cur.execute(
                """INSERT INTO products (tenant_id,id,name,purpose,intended_users,owner_user_id,
                starting_point,created_by,goals,provenance,archived_at)
                VALUES ('tenant',%s,%s,'purpose','[]','owner','idea','owner','[]','[]',%s)""",
                (identity, identity, "2026-01-01" if identity == "empty" else None),
            )
        cur.execute(
            "INSERT INTO projects (tenant_id,id,name,owner_user_id,configuration,provenance) "
            "VALUES ('tenant','original','Original','owner','{}','[]')"
        )
        cur.execute("""INSERT INTO product_project_bindings (tenant_id,id,product_id,project_id,
                relationship_revision,mapping_version,source_connection_id,provenance)
                VALUES ('tenant','binding','existing','original',1,'profile-registry-1','adp.profile','{}')""")
        cur.execute(
            "INSERT INTO orchestration_flows (id,org_id,slug,title,state,product_id) VALUES ('flow','tenant','flow','Flow','pending','existing')"
        )


def test_backfill_preserves_existing_project_and_survives_code_rollback(pg_url):
    upgrade(pg_url, "145_conversation_list_indexes")
    seed(pg_url)
    upgrade(pg_url, "146_project_budgets")
    with psycopg2.connect(pg_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='orchestration_flows'")
        assert {row[0] for row in cur.fetchall()} == set(OrchestrationFlow.__table__.columns.keys())
        cur.execute("SELECT product_id, project_id FROM product_project_bindings WHERE is_base ORDER BY product_id")
        before = cur.fetchall()
        assert len(before) == 2 and before[1] == ("existing", "original")
        cur.execute("SELECT project_id FROM orchestration_flows WHERE id='flow'")
        assert cur.fetchone() == ("original",)
        cur.execute("SELECT name FROM projects WHERE id='original'")
        assert cur.fetchone() == ("Original",)
    downgrade(pg_url, "145_conversation_list_indexes")
    upgrade(pg_url, "146_project_budgets")
    with psycopg2.connect(pg_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT product_id, project_id FROM product_project_bindings WHERE is_base ORDER BY product_id")
        assert cur.fetchall() == before


async def test_concurrent_run_capture_and_settlement_share_one_project(pg_url):
    upgrade(pg_url)
    seed(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ReservationStore(None, 120, client=client)
    scope = ProjectAttribution("tenant", "existing", "original", 1)

    async def bind():
        async with sessions() as db:
            value = await bind_run(db, scope=scope, run_id="run", owner_user_id="owner")
            await db.commit()
            return value

    try:
        captures = await asyncio.gather(*(bind() for _ in range(8)))
        assert len(set(captures)) == 1
        async with sessions() as db:
            assert len((await db.scalars(select(ProjectRun))).all()) == 1
            target = replace(await prepare_project_budget(db, store, scope), headroom_usd=Decimal("1"))
            await db.commit()
        assert (await store.reserve("paid", Decimal(".8"), [target])).admitted

        async def settle():
            async with sessions() as db:
                result = await settle_usage(
                    db,
                    org_id="tenant",
                    request_id="paid",
                    user_id="owner",
                    cost=Decimal(".2"),
                    total_tokens=10,
                    entities=[("project", "original")],
                    reservation_scope_keys=[target.key()],
                )
                await db.commit()
                return result

        settlements = await asyncio.gather(*(settle() for _ in range(8)))
        assert settlements.count(True) == 1
        async with sessions() as db:
            await reconcile_project_budget(db, store, target)
            await reconcile_project_budget(db, store, target)
        assert (await store.snapshot(target)).total_usd == Decimal(".2")
        with pytest.raises(ProjectScopeError):
            async with sessions() as db:
                await bind_run(db, scope=scope, run_id="run", owner_user_id="intruder")
    finally:
        await engine.dispose()
        await client.aclose()
